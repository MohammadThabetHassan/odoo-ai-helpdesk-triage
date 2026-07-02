"""Settings fields for AI Helpdesk Triage."""

from datetime import datetime, time

from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    """Expose Anthropic and governance settings in Odoo Settings."""

    _inherit = "res.config.settings"

    ai_provider = fields.Selection(
        [
            ("anthropic", "Anthropic (direct)"),
            ("bedrock", "Amazon Bedrock"),
        ],
        default="anthropic",
        config_parameter="ai_helpdesk_triage.provider",
        string="LLM Provider",
        help="Which hosted Claude endpoint to call for triage and resolution.",
    )
    anthropic_api_key = fields.Char(
        string="Anthropic API Key",
        config_parameter="ai_helpdesk_triage.anthropic_api_key",
        help="API key used when the provider is Anthropic (direct).",
    )
    bedrock_api_key = fields.Char(
        string="Bedrock API Key",
        config_parameter="ai_helpdesk_triage.bedrock_api_key",
        help=(
            "AWS Bedrock long-lived API key (used as a Bearer token). "
            "Only used when the provider is Bedrock."
        ),
    )
    bedrock_region = fields.Char(
        default="us-east-1",
        config_parameter="ai_helpdesk_triage.bedrock_region",
        string="Bedrock Region",
        help="AWS region hosting the Bedrock runtime endpoint.",
    )
    bedrock_model_id = fields.Char(
        default="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        config_parameter="ai_helpdesk_triage.bedrock_model_id",
        string="Bedrock Model ID",
        help=(
            "Bedrock model identifier for a Claude family model, e.g. "
            "us.anthropic.claude-sonnet-4-5-20250929-v1:0."
        ),
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
    ai_autonomy_level = fields.Selection(
        [
            ("off", "Off (triage only)"),
            ("read_only", "Read-only (lookups + escalation)"),
            ("full", "Full (also perform write actions)"),
        ],
        default="read_only",
        config_parameter="ai_helpdesk_triage.autonomy_level",
        string="AI Autonomy Level",
        help=(
            "Controls what the AI can do after triage. read_only allows the AI "
            "to look things up and escalate; full also lets it send emails and "
            "reset passwords on the categories you approve below."
        ),
    )
    ai_max_actions_per_ticket = fields.Integer(
        default=5,
        config_parameter="ai_helpdesk_triage.max_actions_per_ticket",
        string="Max Actions per Ticket",
        help="Upper bound on tool calls per resolution attempt.",
    )
    ai_action_cost_cap_usd = fields.Float(
        default=0.5,
        config_parameter="ai_helpdesk_triage.action_cost_cap_usd",
        string="Cost Cap per Ticket (USD)",
        help=(
            "Terminate the resolution loop when cumulative cost exceeds this "
            "value. Zero disables the cap."
        ),
    )
    ai_autonomy_categories = fields.Char(
        config_parameter="ai_helpdesk_triage.autonomy_categories",
        string="Categories Approved for Full Autonomy",
        help=(
            "Comma-separated list of categories eligible for full autonomy "
            "(e.g. billing,technical). Ignored when level is off/read_only."
        ),
    )
    ai_auto_resolve_after_triage = fields.Boolean(
        default=False,
        config_parameter="ai_helpdesk_triage.auto_resolve_after_triage",
        string="Auto-run Resolution After Triage",
        help=(
            "When enabled, tickets that pass triage confidence gates trigger "
            "the resolution loop automatically."
        ),
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
