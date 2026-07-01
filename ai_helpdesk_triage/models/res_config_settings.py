"""Settings fields for AI Helpdesk Triage."""

from datetime import datetime, time

from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    """Expose Anthropic and governance settings in Odoo Settings."""

    _inherit = "res.config.settings"

    anthropic_api_key = fields.Char(
        string="Anthropic API Key",
        config_parameter="ai_helpdesk_triage.anthropic_api_key",
        help="API key used to call the Anthropic Messages API.",
    )
    ai_auto_route_confidence_threshold = fields.Float(
        string="Auto-route Confidence Threshold",
        default=0.85,
        config_parameter="ai_helpdesk_triage.auto_route_confidence_threshold",
        help="At or above this confidence, the AI routes the ticket and moves it to AI Triaged.",
    )
    ai_review_confidence_threshold = fields.Float(
        string="Review Confidence Threshold",
        default=0.5,
        config_parameter="ai_helpdesk_triage.review_confidence_threshold",
        help="Below this confidence, the ticket stays New and is flagged for human triage.",
    )
    ai_daily_budget_usd = fields.Float(
        string="Daily AI Budget (USD)",
        config_parameter="ai_helpdesk_triage.daily_budget_usd",
        help="Optional spend guardrail. Leave zero to disable daily budget enforcement.",
    )
    ai_redact_pii = fields.Boolean(
        string="Redact obvious PII before API calls",
        default=True,
        config_parameter="ai_helpdesk_triage.redact_pii",
        help="Redacts obvious emails and phone-like values from ticket text before sending to Anthropic.",
    )
    ai_monthly_spend_usd = fields.Float(
        string="Estimated Spend This Month",
        compute="_compute_ai_spend",
        readonly=True,
    )
    ai_today_spend_usd = fields.Float(
        string="Estimated Spend Today",
        compute="_compute_ai_spend",
        readonly=True,
    )

    def _compute_ai_spend(self):
        """Compute visible spend summaries from ticket-level telemetry."""
        Ticket = self.env["ai.helpdesk.ticket"].sudo()
        today = fields.Date.context_today(self)
        day_start = datetime.combine(today, time.min)
        month_start = datetime.combine(today.replace(day=1), time.min)
        today_cost = sum(
            Ticket.search(
                [("ai_triaged_date", ">=", fields.Datetime.to_string(day_start))],
            ).mapped("ai_total_cost"),
        )
        month_cost = sum(
            Ticket.search(
                [("ai_triaged_date", ">=", fields.Datetime.to_string(month_start))],
            ).mapped("ai_total_cost"),
        )
        for settings in self:
            settings.ai_today_spend_usd = today_cost
            settings.ai_monthly_spend_usd = month_cost
