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
    "You are an autonomous helpdesk resolution agent. Your job is to "
    "RESOLVE the customer's issue end-to-end using the available tools. "
    "Escalation is the LAST resort, not the first choice.\n\n"
    "Decision framework — pick ONE path per ticket:\n"
    "- Password / login issues: call `lookup_customer` to confirm the "
    "account, then call `send_password_reset`, then `post_customer_reply` "
    "to confirm.\n"
    "- Missing / not-received invoice: call `lookup_invoice` to confirm "
    "it exists and is posted, then call `resend_invoice_pdf`, then "
    "`post_customer_reply` to confirm.\n"
    "- Simple product question you can answer from context: call "
    "`post_customer_reply` with the answer.\n"
    "- Data investigation (wrong numbers, broken reports), complex bugs, "
    "or policy questions no tool can resolve: use `lookup_customer` for "
    "context, then `create_team_activity` to brief a specialist, then "
    "`escalate_to_human`.\n\n"
    "Hard rules:\n"
    "1. Read the ticket carefully. Identify the ONE thing the customer "
    "actually wants.\n"
    "2. Take action decisively — do not loop through more lookups than "
    "you need. Two read tools maximum before the resolving action.\n"
    "3. Never repeat a tool call with the same input.\n"
    "4. Call `post_customer_reply` at most once, and only AFTER the "
    "action you took, to confirm what happened.\n"
    "5. End your turn cleanly after a successful write action. Do NOT "
    "call `escalate_to_human` on a ticket a write tool just resolved.\n"
    "6. Only call `escalate_to_human` when a write tool errored and you "
    "cannot work around it, or the customer's request is genuinely not "
    "something your tools can fix.\n"
    "7. Do not fabricate customer data — only use what tools return."
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
        round_cost = anthropic_client.estimate_cost(
            input_tokens,
            output_tokens,
            model_id=payload.get("model") or anthropic_client.DEFAULT_MODEL,
        )
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
    # Sequence must be unique per ticket. Deriving from max()+1 keeps it
    # collision-free across parallel tool_use blocks in one API turn and
    # across reset-and-re-resolve cycles that leave prior rows behind.
    last = env["ai.helpdesk.action"].sudo().search([("ticket_id", "=", ticket.id)], order="sequence desc", limit=1)
    sequence = (last.sequence or 0) + 1
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
    redaction_enabled = ticket._get_bool_param(
        "ai_helpdesk_triage.redact_pii",
        default=True,
    )
    subject = ticket._redact_pii(ticket.name) if redaction_enabled else ticket.name
    description = ticket._redact_pii(ticket.description) if redaction_enabled else ticket.description
    customer_display = ticket.partner_id.display_name if ticket.partner_id else "Unknown"
    if redaction_enabled and ticket.partner_id:
        customer_display = ticket._redact_pii(customer_display)
    # The Customer email line is intentionally not redacted: write tools like
    # send_password_reset and lookup_customer need the real address, and the
    # model reads it from this line when populating tool inputs.
    customer_email = ticket.partner_email or (ticket.partner_id.email if ticket.partner_id else "")
    return (
        f"Ticket #{ticket.id} — {subject}\n\n"
        f"Customer: {customer_display}\n"
        f"Customer email: {customer_email}\n\n"
        f"Description:\n{description}\n\n"
        f"Triage:\n{triaged_summary}\n\n"
        f"Available tools:\n{tool_lines}\n\n"
        "Attempt to resolve or explicitly escalate."
    )
