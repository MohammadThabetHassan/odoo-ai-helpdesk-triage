"""Escalation tools — always available, mark the ticket for human attention."""

from __future__ import annotations

from datetime import timedelta

from odoo import fields

CREATE_ACTIVITY_SCHEMA = {
    "name": "create_team_activity",
    "description": (
        "LAST-RESORT tool. Schedule a to-do activity on the ticket for a "
        "human specialist when NO other tool can resolve the request. Only "
        "use for data investigations (wrong numbers, broken reports), "
        "complex bugs, or product-knowledge questions no other tool can "
        "answer. Do NOT use this for password resets, invoice resends, or "
        "simple factual answers — those have dedicated write tools that you "
        "MUST use instead."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "team_name": {"type": "string", "minLength": 1},
            "deadline_days": {
                "type": "integer",
                "minimum": 0,
                "maximum": 30,
                "default": 1,
            },
            "summary": {"type": "string", "minLength": 1, "maxLength": 200},
            "note": {"type": "string", "minLength": 1, "maxLength": 2000},
        },
        "required": ["team_name", "summary", "note"],
    },
}

ESCALATE_TO_HUMAN_SCHEMA = {
    "name": "escalate_to_human",
    "description": (
        "TERMINAL LAST-RESORT tool. Ends the loop and marks the ticket as "
        "escalated. Only call this if a write tool actually errored and you "
        "cannot work around it, or the request is genuinely outside every "
        "tool's capability. Do NOT call this after a successful write action "
        "— end your turn cleanly instead. Do NOT call this because you feel "
        "uncertain; use a lookup tool first."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "reason": {"type": "string", "minLength": 1, "maxLength": 1000},
        },
        "required": ["reason"],
    },
}


def create_team_activity(env, ticket, team_name, summary, note, deadline_days=1):
    """Schedule a to-do activity on the ticket for a team member."""
    team = (
        env["ai.helpdesk.team"]
        .sudo()
        .search(
            [("name", "=ilike", team_name), ("active", "=", True)],
            limit=1,
        )
    )
    if not team:
        return {"ok": False, "error": "team_not_found", "team_name": team_name}
    assignee = team.member_ids[:1] if team.member_ids else env["res.users"]
    deadline = fields.Date.context_today(ticket) + timedelta(
        days=max(0, min(int(deadline_days or 1), 30)),
    )
    activity = ticket.sudo().activity_schedule(
        act_type_xmlid="mail.mail_activity_data_todo",
        date_deadline=deadline,
        summary=summary[:200],
        note=note[:2000],
        user_id=assignee.id if assignee else False,
    )
    ticket.with_context(ai_skip_correction_log=True).sudo().write({"team_id": team.id})
    return {
        "ok": True,
        "data": {
            "team_id": team.id,
            "team_name": team.name,
            "activity_id": activity.id if activity else False,
            "assignee": assignee.login if assignee else False,
            "deadline": str(deadline),
        },
    }


def escalate_to_human(env, ticket, reason):
    """Signal the agent loop to stop and mark the ticket as escalated."""
    return {
        "ok": True,
        "terminal": True,
        "data": {"reason": reason[:1000]},
    }


ESCALATION_TOOLS = {
    "create_team_activity": {
        "schema": CREATE_ACTIVITY_SCHEMA,
        "callable": create_team_activity,
        "class": "escalation",
    },
    "escalate_to_human": {
        "schema": ESCALATE_TO_HUMAN_SCHEMA,
        "callable": escalate_to_human,
        "class": "escalation",
        "terminal": True,
    },
}
