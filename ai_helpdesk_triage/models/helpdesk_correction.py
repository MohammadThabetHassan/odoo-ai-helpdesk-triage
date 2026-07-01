"""Human correction history for future evaluation datasets."""

from odoo import fields, models


class HelpdeskCorrection(models.Model):
    """A labeled example captured when a human overrides an AI decision."""

    _name = "ai.helpdesk.correction"
    _description = "AI Helpdesk Human Correction"
    _order = "corrected_at desc, id desc"

    ticket_id = fields.Many2one(
        "ai.helpdesk.ticket",
        required=True,
        ondelete="cascade",
        index=True,
    )
    corrected_at = fields.Datetime(
        default=fields.Datetime.now,
        required=True,
        index=True,
    )
    user_id = fields.Many2one(
        "res.users",
        required=True,
        default=lambda self: self.env.user,
    )
    field_name = fields.Selection(
        [
            ("category", "Category"),
            ("priority", "Priority"),
            ("team_id", "Team"),
        ],
        required=True,
        index=True,
    )
    ai_value = fields.Char(required=True)
    human_value = fields.Char(required=True)
    ai_confidence = fields.Float(digits=(3, 2))
    ticket_name = fields.Char(required=True)
    ticket_description = fields.Text(required=True)
    ai_reasoning = fields.Text()

    def action_open_ticket(self):
        """Open the corrected ticket from dashboard rows."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": self.ticket_id.display_name,
            "res_model": "ai.helpdesk.ticket",
            "res_id": self.ticket_id.id,
            "view_mode": "form",
        }
