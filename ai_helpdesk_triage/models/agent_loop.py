"""Multi-turn Anthropic tool-use loop that drives agentic ticket resolution."""

from __future__ import annotations

import hashlib
import json
import logging
import time

from odoo import _
from odoo.exceptions import UserError

from . import anthropic_client
from .tool_registry import get_available_tools, get_tool_schemas

_logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are an autonomous helpdesk resolution agent. You have already "
    "classified this ticket. Your job now is to resolve the customer's "
    "issue by calling the available tools, or to explicitly escalate "
    "when you cannot.\n\n"
    "Rules:\n"
    "1. Prefer read-only lookups before taking write actions.\n"
    "2. Never repeat the same tool call with the same input.\n"
    "3. Call `post_customer_reply` at most once, to confirm the outcome.\n"
    "4. When you are done, call `escalate_to_human` if you could not "
    "resolve the issue, or end your turn cleanly if you did resolve it.\n"
    "5. Do not fabricate customer data — only use what tools return."
)


class AgentLoopResult:
    """Simple container so callers don't depend on a dict schema."""

    def __init__(self, status, reasoning, actions, cost, tokens, reason=None):
        self.status = status  # resolved / escalated / failed
        self.reasoning = reasoning
        self.actions = actions
        self.cost = cost
        self.tokens = tokens
        self.reason = reason


def run(env, ticket, autonomy_level, max_actions, cost_cap):
    """Execute the multi-turn resolution loop for a single ticket.

    Returns AgentLoopResult. Does not commit — the caller handles state writes.
    """
    tools = get_available_tools(env, autonomy_level)
    if not tools:
        return AgentLoopResult(
            status="escalated",
            reasoning=_("Autonomy is disabled; nothing to attempt."),
            actions=[],
            cost=0.0,
            tokens={"input": 0, "output": 0},
            reason="autonomy_off",
        )

    schemas = get_tool_schemas(env, autonomy_level)
    messages = [
        {"role": "user", "content": _initial_user_message(ticket, tools)},
    ]

    actions = []
    total_cost = 0.0
    total_tokens = {"input": 0, "output": 0}
    final_text = ""
    seen_calls = set()

    for iteration in range(1, max_actions + 1):
        payload = {
            "model": anthropic_client.DEFAULT_MODEL,
            "max_tokens": anthropic_client.DEFAULT_MAX_TOKENS,
            "system": SYSTEM_PROMPT,
            "tools": schemas,
            "messages": messages,
        }
        started = time.monotonic()
        response = anthropic_client.post_message(env, payload)
        elapsed_ms = int((time.monotonic() - started) * 1000)

        usage = response.get("usage") or {}
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        round_cost = anthropic_client.estimate_cost(input_tokens, output_tokens)
        total_cost += round_cost
        total_tokens["input"] += input_tokens
        total_tokens["output"] += output_tokens

        if cost_cap > 0 and total_cost > cost_cap:
            return AgentLoopResult(
                status="escalated",
                reasoning=final_text or _("Cost cap reached before the AI could finish."),
                actions=actions,
                cost=total_cost,
                tokens=total_tokens,
                reason="cost_cap",
            )

        stop_reason = response.get("stop_reason")
        content_blocks = response.get("content") or []
        assistant_text = _collect_text(content_blocks)
        if assistant_text:
            final_text = assistant_text

        tool_uses = [b for b in content_blocks if b.get("type") == "tool_use"]
        if not tool_uses:
            # Assistant ended turn without tool call — treat as resolution
            # if we already ran some actions, else as escalation.
            status = "resolved" if actions else "escalated"
            reason = None if actions else "no_tool_use"
            messages.append(
                {"role": "assistant", "content": content_blocks},
            )
            return AgentLoopResult(
                status=status,
                reasoning=final_text,
                actions=actions,
                cost=total_cost,
                tokens=total_tokens,
                reason=reason,
            )

        messages.append({"role": "assistant", "content": content_blocks})
        tool_results = []
        terminal_hit = False
        terminal_reason = None

        for block in tool_uses:
            name = block.get("name")
            tool_input = block.get("input") or {}
            use_id = block.get("id")
            call_hash = _hash_call(name, tool_input)

            if call_hash in seen_calls:
                result = {"ok": False, "error": "duplicate_call_blocked"}
                _record_action(
                    env,
                    ticket,
                    iteration,
                    name,
                    tool_input,
                    result,
                    assistant_text,
                    input_tokens,
                    output_tokens,
                    round_cost,
                    elapsed_ms,
                )
                tool_results.append(_tool_result_message(use_id, result))
                continue

            seen_calls.add(call_hash)
            tool = tools.get(name)
            if not tool:
                result = {"ok": False, "error": "tool_not_available", "name": name}
            else:
                result = _execute_tool(env, ticket, tool, tool_input)
                if tool.get("terminal") and result.get("ok"):
                    terminal_hit = True
                    terminal_reason = (result.get("data") or {}).get(
                        "reason",
                        "escalated_by_ai",
                    )

            action = _record_action(
                env,
                ticket,
                iteration,
                name,
                tool_input,
                result,
                assistant_text,
                input_tokens,
                output_tokens,
                round_cost,
                elapsed_ms,
            )
            actions.append(action)
            tool_results.append(_tool_result_message(use_id, result))

        messages.append({"role": "user", "content": tool_results})

        if terminal_hit:
            return AgentLoopResult(
                status="escalated",
                reasoning=final_text or _("Agent escalated to human."),
                actions=actions,
                cost=total_cost,
                tokens=total_tokens,
                reason=terminal_reason or "escalated_by_ai",
            )

        if stop_reason == "end_turn":
            return AgentLoopResult(
                status="resolved" if actions else "escalated",
                reasoning=final_text,
                actions=actions,
                cost=total_cost,
                tokens=total_tokens,
                reason=None if actions else "no_tool_use",
            )

    return AgentLoopResult(
        status="escalated",
        reasoning=final_text or _("Max action count reached without resolution."),
        actions=actions,
        cost=total_cost,
        tokens=total_tokens,
        reason="max_actions",
    )


def _execute_tool(env, ticket, tool, tool_input):
    """Run a tool inside a savepoint so failures don't poison the loop."""
    callable_ = tool["callable"]
    try:
        with env.cr.savepoint():
            return callable_(env, ticket, **tool_input)
    except TypeError as exc:
        return {"ok": False, "error": "bad_arguments", "detail": str(exc)}
    except UserError as exc:
        return {"ok": False, "error": "user_error", "detail": str(exc)}
    except Exception as exc:  # noqa: BLE001
        _logger.exception("Tool %s failed on ticket %s", tool["schema"]["name"], ticket.id)
        return {"ok": False, "error": "unhandled_exception", "detail": str(exc)}


def _record_action(
    env,
    ticket,
    sequence,
    name,
    tool_input,
    result,
    reasoning,
    input_tokens,
    output_tokens,
    cost,
    duration_ms,
):
    """Persist a single tool invocation to the audit log."""
    return (
        env["ai.helpdesk.action"]
        .sudo()
        .create(
            {
                "ticket_id": ticket.id,
                "sequence": sequence,
                "tool_name": name,
                "tool_input": json.dumps(tool_input, ensure_ascii=True),
                "tool_result": json.dumps(result, ensure_ascii=True, default=str),
                "succeeded": bool(result.get("ok")),
                "error_message": result.get("error") if not result.get("ok") else False,
                "ai_reasoning": reasoning or False,
                "ai_confidence": ticket.ai_confidence,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost": cost,
                "duration_ms": duration_ms,
            },
        )
    )


def _tool_result_message(use_id, result):
    """Format a tool_result content block for the next Anthropic turn."""
    return {
        "type": "tool_result",
        "tool_use_id": use_id,
        "content": json.dumps(result, ensure_ascii=True, default=str),
    }


def _collect_text(content_blocks):
    """Extract concatenated text blocks from an assistant response."""
    return "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text").strip()


def _hash_call(name, tool_input):
    """Stable hash of (tool name, canonical input) for repetition detection."""
    canonical = json.dumps(tool_input or {}, sort_keys=True, ensure_ascii=True)
    return hashlib.sha1(f"{name}:{canonical}".encode("utf-8")).hexdigest()


def _initial_user_message(ticket, tools):
    """Compose the opening message describing the ticket + available tools."""
    tool_lines = "\n".join(f"- {name}: {tool['schema']['description']}" for name, tool in tools.items())
    triaged_summary = (
        f"Category: {ticket.category or 'unknown'}\n"
        f"Priority: {ticket.priority or '1'}\n"
        f"Triage confidence: {ticket.ai_confidence:.2f}\n"
        f"Triage reasoning: {ticket.ai_reasoning or 'n/a'}"
    )
    return (
        f"Ticket #{ticket.id} — {ticket.name}\n\n"
        f"Customer: {ticket.partner_id.display_name if ticket.partner_id else 'Unknown'}\n"
        f"Customer email: {ticket.partner_email or (ticket.partner_id.email if ticket.partner_id else '')}\n\n"
        f"Description:\n{ticket.description}\n\n"
        f"Triage:\n{triaged_summary}\n\n"
        f"Available tools:\n{tool_lines}\n\n"
        "Attempt to resolve or explicitly escalate."
    )
