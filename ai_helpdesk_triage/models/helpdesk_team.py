"""Support team model used by the AI routing layer."""

from odoo import _, api, fields, models


class HelpdeskTeam(models.Model):
    """Operational team that can own AI-triaged support tickets."""

    _name = "ai.helpdesk.team"
    _description = "AI Helpdesk Team"
    _order = "name"

    name = fields.Char(required=True)
    description = fields.Text()
    member_ids = fields.Many2many(
        "res.users",
        "ai_helpdesk_team_user_rel",
        "team_id",
        "user_id",
        string="Team Members",
    )
    active = fields.Boolean(default=True)
    color = fields.Integer()
    ticket_count = fields.Integer(compute="_compute_ticket_count", string="Tickets")

    @api.depends("name")
    def _compute_ticket_count(self):
        """Count assigned tickets without storing denormalized state."""
        Ticket = self.env["ai.helpdesk.ticket"]
        for team in self:
            team.ticket_count = Ticket.search_count([("team_id", "=", team.id)])

    def action_view_tickets(self):
        """Open tickets routed to this team with a default for new records."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Tickets"),
            "res_model": "ai.helpdesk.ticket",
            "view_mode": "kanban,list,form",
            "domain": [("team_id", "=", self.id)],
            "context": {"default_team_id": self.id},
        }
