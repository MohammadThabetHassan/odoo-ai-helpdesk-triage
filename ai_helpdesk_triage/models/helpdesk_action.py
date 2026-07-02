"""Audit log of every tool call made by the agentic resolution loop."""

from odoo import fields, models


class HelpdeskAction(models.Model):
    """One row per tool invocation during agentic resolution."""

    _name = "ai.helpdesk.action"
    _description = "AI Helpdesk Agent Action"
    _order = "ticket_id, sequence, id"

    ticket_id = fields.Many2one(
        "ai.helpdesk.ticket",
        required=True,
        ondelete="cascade",
        index=True,
    )
    sequence = fields.Integer(default=0, index=True)
    tool_name = fields.Char(required=True, index=True)
    tool_input = fields.Text(string="Tool Input (JSON)")
    tool_result = fields.Text(string="Tool Result (JSON)")
    succeeded = fields.Boolean(default=False)
    error_message = fields.Text()
    ai_reasoning = fields.Text(string="AI Reasoning")
    ai_confidence = fields.Float(digits=(3, 2))
    input_tokens = fields.Integer()
    output_tokens = fields.Integer()
    cost = fields.Float(digits=(10, 4), string="Estimated Cost")
    duration_ms = fields.Integer(string="Duration (ms)")

    _sql_constraints = [
        (
            "action_ticket_sequence_unique",
            "unique(ticket_id, sequence)",
            "Two agent actions cannot share the same sequence on a ticket.",
        ),
    ]

    def action_open_ticket(self):
        """Open the parent ticket in form view."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": self.ticket_id.display_name,
            "res_model": "ai.helpdesk.ticket",
            "res_id": self.ticket_id.id,
            "view_mode": "form",
        }
