"""Registry of tools available to the agentic resolution loop."""

from __future__ import annotations

from .tools.escalation_tools import ESCALATION_TOOLS
from .tools.read_tools import READ_TOOLS
from .tools.reputation_tools import REPUTATION_TOOLS
from .tools.retrieval_tools import RETRIEVAL_TOOLS
from .tools.write_tools import WRITE_TOOLS

TOOL_REGISTRY = {
    **READ_TOOLS,
    **REPUTATION_TOOLS,
    **RETRIEVAL_TOOLS,
    **WRITE_TOOLS,
    **ESCALATION_TOOLS,
}


AUTONOMY_CLASSES = {
    "off": set(),
    "read_only": {"read", "escalation"},
    "full": {"read", "write", "escalation"},
}


def get_available_tools(env, autonomy_level):
    """Return tool metadata filtered by autonomy level, kill-switch, and module availability."""
    allowed_classes = AUTONOMY_CLASSES.get(autonomy_level, set())
    if not allowed_classes:
        return {}
    disabled = _get_disabled_tools(env)
    redact_pii = _get_redact_pii(env)
    result = {}
    for name, tool in TOOL_REGISTRY.items():
        if name in disabled:
            continue
        if tool["class"] not in allowed_classes:
            continue
        required_module = tool.get("requires_module")
        if required_module and not _module_installed(env, required_module):
            continue
        # PII-dependent tools need the raw ticket text to be visible to the
        # model. When redact_pii is on, the model only sees [REDACTED_EMAIL]
        # / [REDACTED_PHONE] placeholders, so it cannot pass a real new
        # value — the tool becomes structurally unusable. Hide it from the
        # schema entirely instead of letting the model waste iterations
        # trying to call it. Operators who want the tool must turn off
        # redact_pii deliberately.
        if redact_pii and tool.get("requires_raw_pii"):
            continue
        result[name] = tool
    return result


def get_tool_schemas(env, autonomy_level):
    """Return the list of Anthropic tool schemas for the current autonomy."""
    return [tool["schema"] for tool in get_available_tools(env, autonomy_level).values()]


def _get_disabled_tools(env):
    """Read the operator-managed kill-switch list of tool names."""
    raw = env["ir.config_parameter"].sudo().get_param("ai_helpdesk_triage.disabled_tools", "") or ""
    return {name.strip() for name in raw.split(",") if name.strip()}


def _get_redact_pii(env):
    """Read the operator's PII redaction toggle."""
    raw = env["ir.config_parameter"].sudo().get_param("ai_helpdesk_triage.redact_pii", "True")
    if isinstance(raw, bool):
        return raw
    return str(raw).lower() in {"1", "true", "yes", "on"}


def _module_installed(env, name):
    return bool(
        env["ir.module.module"].sudo().search_count([("name", "=", name), ("state", "=", "installed")]),
    )
