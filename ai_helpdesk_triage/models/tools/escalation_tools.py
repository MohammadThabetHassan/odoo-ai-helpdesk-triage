"""Escalation tools — always available, mark the ticket for human attention."""

from __future__ import annotations

from datetime import timedelta

from odoo import fields

CREATE_ACTIVITY_SCHEMA = {
    "name": "create_team_activity",
    "description": (
        "Schedule a to-do activity on the ticket for a member of the given team. "
        "Use this to route the ticket to a human specialist with context and a "
        "deadline. Prefer this over escalate_to_human when you know which team "
        "should handle the ticket."
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
        "Give up autonomous resolution and escalate the ticket to a human. Use "
        "this when you cannot resolve the issue safely or need information you "
        "don't have. This ends the agent loop."
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
    ticket.sudo().write({"team_id": team.id})
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
