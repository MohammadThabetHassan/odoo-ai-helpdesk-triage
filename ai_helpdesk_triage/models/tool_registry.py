"""Registry of tools available to the agentic resolution loop."""

from __future__ import annotations

from .tools.escalation_tools import ESCALATION_TOOLS
from .tools.read_tools import READ_TOOLS
from .tools.reputation_tools import REPUTATION_TOOLS
from .tools.write_tools import WRITE_TOOLS

TOOL_REGISTRY = {
    **READ_TOOLS,
    **REPUTATION_TOOLS,
    **WRITE_TOOLS,
    **ESCALATION_TOOLS,
}

AUTONOMY_CLASSES = {
    "off": set(),
    "read_only": {"read", "escalation"},
    "full": {"read", "write", "escalation"},
}


def get_available_tools(env, autonomy_level):
    """Return tool metadata filtered by autonomy level and module availability."""
    allowed_classes = AUTONOMY_CLASSES.get(autonomy_level, set())
    if not allowed_classes:
        return {}

    result = {}
    for name, tool in TOOL_REGISTRY.items():
        if tool["class"] not in allowed_classes:
            continue
        required_module = tool.get("requires_module")
        if required_module and not _module_installed(env, required_module):
            continue
        result[name] = tool
    return result


def get_tool_schemas(env, autonomy_level):
    """Return the list of Anthropic tool schemas for the current autonomy."""
    return [tool["schema"] for tool in get_available_tools(env, autonomy_level).values()]


def _module_installed(env, name):
    return bool(
        env["ir.module.module"].sudo().search_count([("name", "=", name), ("state", "=", "installed")]),
    )