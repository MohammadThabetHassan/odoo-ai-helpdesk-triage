"""Ticket workflow and Anthropic triage integration."""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta
from datetime import time as datetime_time

from markupsafe import Markup, escape
from odoo import _, api, fields, models
from odoo.exceptions import UserError

from . import agent_loop, anthropic_client

_logger = logging.getLogger(__name__)

try:
    import requests
except ImportError:  # pragma: no cover - covered by runtime guard
    requests = None

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"
ANTHROPIC_MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 1000
REQUEST_TIMEOUT = (5, 45)
MAX_RETRIES = 3
BACKOFF_SECONDS = 1.5
TRANSIENT_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}

CATEGORY_SELECTION = [
    ("technical", "Technical"),
    ("billing", "Billing"),
    ("general", "General"),
    ("feature_request", "Feature Request"),
]
PRIORITY_SELECTION = [
    ("0", "Low"),
    ("1", "Medium"),
    ("2", "High"),
    ("3", "Urgent"),
]
REVIEW_STATUS_SELECTION = [
    ("none", "No Review Flag"),
    ("accepted", "Accepted"),
    ("review_recommended", "Review Recommended"),
    ("needs_human", "Needs Human"),
]
RESOLUTION_STATUS_SELECTION = [
    ("not_attempted", "Not Attempted"),
    ("in_progress", "In Progress"),
    ("resolved", "AI Resolved"),
    ("escalated", "Escalated"),
    ("failed", "Failed"),
]
SENTIMENT_SELECTION = [
    ("neutral", "Neutral"),
    ("frustrated", "Frustrated"),
    ("angry", "Angry"),
]
URGENCY_SELECTION = [
    ("low", "Low"),
    ("normal", "Normal"),
    ("vip", "VIP"),
]
CORRECTION_FIELDS = {"category", "priority", "team_id"}
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{7,}\d)(?!\w)")


class HelpdeskTicket(models.Model):
    """Support ticket with an AI-first, human-reviewed triage workflow."""

    _name = "ai.helpdesk.ticket"
    _description = "AI Helpdesk Ticket"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "create_date desc, id desc"
    _rec_name = "name"

    name = fields.Char(string="Subject", required=True, tracking=True)
    description = fields.Text(required=True, tracking=True)
    partner_id = fields.Many2one("res.partner", string="Customer", tracking=True)
    partner_email = fields.Char(string="Customer Email")

    state = fields.Selection(
        [
            ("new", "New"),
            ("ai_triaged", "AI Triaged"),
            ("assigned", "Assigned"),
            ("in_progress", "In Progress"),
            ("resolved", "Resolved"),
            ("closed", "Closed"),
        ],
        default="new",
        required=True,
        tracking=True,
        group_expand="_expand_states",
    )
    category = fields.Selection(CATEGORY_SELECTION, tracking=True)
    priority = fields.Selection(
        PRIORITY_SELECTION,
        default="1",
        required=True,
        tracking=True,
    )
    team_id = fields.Many2one("ai.helpdesk.team", string="Assigned Team", tracking=True)
    user_id = fields.Many2one("res.users", string="Assigned To", tracking=True)

    ai_triaged = fields.Boolean(default=False, readonly=True, string="AI Triaged")
    ai_triage_attempted = fields.Boolean(default=False, readonly=True)
    ai_triage_in_progress = fields.Boolean(default=False, readonly=True)
    ai_triaged_date = fields.Datetime(readonly=True)
    ai_reasoning = fields.Text(readonly=True, string="AI Reasoning")
    ai_suggested_reply = fields.Text(string="AI Suggested Reply")
    ai_confidence = fields.Float(readonly=True, digits=(3, 2), string="AI Confidence")
    ai_confidence_percent = fields.Integer(
        compute="_compute_ai_confidence_percent",
        string="AI Confidence %",
    )
    ai_review_status = fields.Selection(
        REVIEW_STATUS_SELECTION,
        default="none",
        readonly=True,
        string="AI Review Status",
    )
    ai_review_note = fields.Text(readonly=True, string="AI Review Note")
    ai_error = fields.Text(readonly=True, string="AI Error")

    ai_sentiment = fields.Selection(
        SENTIMENT_SELECTION,
        readonly=True,
        default="neutral",
        string="Customer Sentiment",
        help="AI-inferred emotional tone of the ticket. Feeds routing and reflection gates.",
    )
    ai_urgency = fields.Selection(
        URGENCY_SELECTION,
        readonly=True,
        default="normal",
        string="Ticket Urgency",
        help="AI-inferred urgency of the ticket. VIP always uses the most capable model.",
    )
    ai_ambiguous = fields.Boolean(
        readonly=True,
        default=False,
        string="Ambiguous Request",
        help="AI flag when the ticket does not clearly describe one action to take.",
    )

    ai_model = fields.Char(readonly=True, string="AI Model")
    ai_input_tokens = fields.Integer(readonly=True)
    ai_output_tokens = fields.Integer(readonly=True)
    ai_total_cost = fields.Float(
        readonly=True,
        digits=(12, 6),
        string="Estimated AI Cost",
    )

    ai_original_category = fields.Selection(CATEGORY_SELECTION, readonly=True)
    ai_original_priority = fields.Selection(PRIORITY_SELECTION, readonly=True)
    ai_original_team_id = fields.Many2one("ai.helpdesk.team", readonly=True)

    ai_resolution_status = fields.Selection(
        RESOLUTION_STATUS_SELECTION,
        default="not_attempted",
        readonly=True,
        tracking=True,
        string="AI Resolution Status",
    )
    ai_resolution_attempts = fields.Integer(readonly=True, default=0)
    ai_resolution_cost = fields.Float(readonly=True, digits=(10, 4))
    ai_resolution_reasoning = fields.Text(readonly=True, string="AI Resolution Notes")
    ai_resolution_reason = fields.Char(
        readonly=True,
        string="Termination Reason",
        help="Machine-readable reason the resolution loop ended.",
    )
    ai_resolution_start_at = fields.Datetime(
        readonly=True,
        string="Resolution Started At",
    )
    ai_resolution_end_at = fields.Datetime(
        readonly=True,
        string="Resolution Ended At",
    )
    ai_resolution_duration_minutes = fields.Float(
        compute="_compute_resolution_duration",
        store=True,
        string="Resolution Duration (min)",
    )
    ai_resolution_in_progress = fields.Boolean(default=False, readonly=True)
    action_ids = fields.One2many(
        "ai.helpdesk.action",
        "ticket_id",
        string="AI Actions",
    )
    action_count = fields.Integer(compute="_compute_action_count", string="Actions")

    color = fields.Integer()

    @api.depends("action_ids")
    def _compute_action_count(self):
        """Count agent actions on this ticket for the smart button."""
        for ticket in self:
            ticket.action_count = len(ticket.action_ids)

    @api.depends("ai_resolution_start_at", "ai_resolution_end_at")
    def _compute_resolution_duration(self):
        """Derive resolution latency in minutes from start/end timestamps."""
        for ticket in self:
            if ticket.ai_resolution_start_at and ticket.ai_resolution_end_at:
                delta = ticket.ai_resolution_end_at - ticket.ai_resolution_start_at
                ticket.ai_resolution_duration_minutes = delta.total_seconds() / 60.0
            else:
                ticket.ai_resolution_duration_minutes = 0.0

    @api.model
    def _expand_states(self, states, domain):
        """Keep every workflow state visible in grouped kanban/list views."""
        return [key for key, _label in self._fields["state"].selection]

    @api.depends("ai_confidence")
    def _compute_ai_confidence_percent(self):
        """Expose 0-1 confidence as a 0-100 progress-bar value."""
        for ticket in self:
            ticket.ai_confidence_percent = round((ticket.ai_confidence or 0.0) * 100)

    @api.onchange("partner_id")
    def _onchange_partner_id(self):
        """Copy the customer email for quick ticket context."""
        for ticket in self:
            ticket.partner_email = ticket.partner_id.email or False

    def write(self, vals):
        """Persist changes and log human overrides of AI decisions."""
        correction_values = self._prepare_correction_values(vals)
        result = super().write(vals)
        if correction_values:
            self.env["ai.helpdesk.correction"].sudo().create(correction_values)
        return result

    def action_ai_triage(self):
        """Run a synchronous AI triage pass on new tickets only."""
        low_confidence_names = []
        for ticket in self:
            ticket._lock_for_triage()
            ticket._ensure_can_triage()
            ticket.with_context(ai_skip_correction_log=True).write(
                {"ai_triage_in_progress": True, "ai_error": False},
            )
            try:
                result = ticket._call_ai_triage_agent()
                ticket._apply_ai_triage_result(result)
                ticket._post_ai_triage_message(result)
                if not result["should_transition"]:
                    low_confidence_names.append(ticket.display_name)
            except UserError as exc:
                ticket.with_context(ai_skip_correction_log=True).write(
                    {"ai_error": str(exc), "ai_triage_in_progress": False},
                )
                raise
            except Exception as exc:  # noqa: BLE001
                _logger.exception("AI triage failed for ticket %s", ticket.id)
                ticket.with_context(ai_skip_correction_log=True).write(
                    {"ai_error": str(exc), "ai_triage_in_progress": False},
                )
                raise UserError(_("AI triage failed: %s") % str(exc)) from exc

        if low_confidence_names:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Human review needed"),
                    "message": _(
                        "AI confidence was below the configured threshold for: %s",
                    )
                    % ", ".join(low_confidence_names),
                    "type": "warning",
                    "sticky": True,
                },
            }
        return True

    def action_ai_resolve(self):
        """Run the agentic resolution loop on triaged tickets."""
        for ticket in self:
            ticket._lock_for_resolve()
            ticket._ensure_can_resolve()
            autonomy_level = ticket._get_autonomy_level()
            if autonomy_level == "off":
                raise UserError(
                    _("Autonomous resolution is disabled in AI Helpdesk settings."),
                )
            if autonomy_level == "full":
                ticket._ensure_category_allowed_for_full_autonomy()
            ticket._guard_daily_budget()
            ticket._check_circuit_breaker()

            ticket.with_context(ai_skip_correction_log=True).write(
                {
                    "ai_resolution_in_progress": True,
                    "ai_resolution_status": "in_progress",
                    "ai_resolution_start_at": fields.Datetime.now(),
                },
            )

            max_actions = int(
                ticket._get_float_param(
                    "ai_helpdesk_triage.max_actions_per_ticket",
                    5,
                    minimum=1,
                    maximum=20,
                ),
            )
            cost_cap = ticket._get_float_param(
                "ai_helpdesk_triage.action_cost_cap_usd",
                0.5,
                minimum=0.0,
            )
            try:
                result = agent_loop.run(
                    ticket.env,
                    ticket,
                    autonomy_level,
                    max_actions,
                    cost_cap,
                )
            except UserError:
                ticket.with_context(ai_skip_correction_log=True).write(
                    {
                        "ai_resolution_in_progress": False,
                        "ai_resolution_status": "failed",
                    },
                )
                raise
            except Exception as exc:  # noqa: BLE001
                _logger.exception("Agent loop failed for ticket %s", ticket.id)
                ticket.with_context(ai_skip_correction_log=True).write(
                    {
                        "ai_resolution_in_progress": False,
                        "ai_resolution_status": "failed",
                    },
                )
                raise UserError(_("Agent resolution failed: %s") % str(exc)) from exc

            ticket._apply_agent_loop_result(result)
            ticket._post_ai_resolution_message(result)
        return True

    def _lock_for_resolve(self):
        """Serialize resolution attempts to prevent concurrent runs."""
        self.ensure_one()
        self.env.cr.execute(
            f"SELECT id FROM {self._table} WHERE id = %s FOR UPDATE",
            [self.id],
        )
        if hasattr(self, "invalidate_recordset"):
            self.invalidate_recordset(
                ["ai_resolution_status", "ai_resolution_in_progress"],
            )

    def _ensure_can_resolve(self):
        """Reject invalid resolution attempts before calling the API."""
        self.ensure_one()
        if self.state != "ai_triaged":
            raise UserError(
                _("Only AI-triaged tickets can be resolved by the agent."),
            )
        if self.ai_resolution_in_progress:
            raise UserError(_("Resolution is already running for this ticket."))
        if self.ai_resolution_status not in ("not_attempted", "failed"):
            raise UserError(
                _("This ticket already has a resolution attempt (%s).") % self.ai_resolution_status,
            )

    def _ensure_category_allowed_for_full_autonomy(self):
        """Guard full-autonomy writes to configured categories only."""
        self.ensure_one()
        raw = self.env["ir.config_parameter"].sudo().get_param("ai_helpdesk_triage.autonomy_categories", "") or ""
        allowed = {item.strip() for item in raw.split(",") if item.strip()}
        if not allowed:
            raise UserError(
                _(
                    "Full autonomy is enabled but no categories are approved. "
                    "Add categories in AI Helpdesk settings.",
                ),
            )
        if self.category not in allowed:
            raise UserError(
                _("Category %s is not approved for autonomous resolution.") % (self.category or "unknown"),
            )

    def _get_autonomy_level(self):
        """Read autonomy level from ir.config_parameter."""
        value = self.env["ir.config_parameter"].sudo().get_param("ai_helpdesk_triage.autonomy_level", "read_only")
        return value if value in ("off", "read_only", "full") else "read_only"

    def _apply_agent_loop_result(self, result):
        """Persist the outcome of an agent loop to the ticket."""
        self.ensure_one()
        self.with_context(ai_skip_correction_log=True).write(
            {
                "ai_resolution_status": result.status,
                "ai_resolution_in_progress": False,
                "ai_resolution_attempts": (self.ai_resolution_attempts or 0) + 1,
                "ai_resolution_cost": (self.ai_resolution_cost or 0.0) + result.cost,
                "ai_resolution_reasoning": result.reasoning or False,
                "ai_resolution_reason": result.reason or False,
                "ai_resolution_end_at": fields.Datetime.now(),
            },
        )

    def _post_agent_iteration_note(self, iteration, model_id, assistant_text, tool_uses):
        """Post an internal-only chatter note summarizing one loop iteration.

        Makes the resolution loop feel agentic to a viewer instead of a
        black-box RPC: iteration number, which model handled the turn, the
        assistant's own reasoning, and which tools it decided to call.
        """
        self.ensure_one()
        if not assistant_text and not tool_uses:
            return
        pill = "info" if model_id and "haiku" in model_id else "primary"
        header = Markup("<span class='badge text-bg-%s'>%s</span>") % (
            pill,
            escape(model_id or "unknown"),
        )
        body = Markup("<p><strong>AI iteration %d</strong> &nbsp;%s</p>") % (
            iteration,
            header,
        )
        if assistant_text:
            body += Markup("<p>%s</p>") % escape(assistant_text)
        if tool_uses:
            body += Markup("<p><em>Tool calls:</em></p><ul>")
            for use in tool_uses:
                name = use.get("name") or "?"
                inp = use.get("input") or {}
                body += Markup("<li><code>%s</code>(%s)</li>") % (
                    escape(name),
                    escape(json.dumps(inp, ensure_ascii=True)[:200]),
                )
            body += Markup("</ul>")
        self.sudo().message_post(
            body=body,
            message_type="notification",
            subtype_xmlid="mail.mt_note",
        )

    def _post_ai_resolution_message(self, result):
        """Post a chatter summary of the agent loop result."""
        self.ensure_one()
        title = (
            _("AI resolution complete")
            if result.status == "resolved"
            else _(
                "AI resolution ended: %s",
            )
            % result.status
        )
        body = Markup(
            "<p><strong>%s</strong></p>"
            "<ul>"
            "<li>Actions taken: %s</li>"
            "<li>Termination reason: %s</li>"
            "</ul>"
            "<p><strong>Agent notes:</strong> %s</p>",
        ) % (
            escape(title),
            len(result.actions),
            escape(result.reason or "-"),
            escape(result.reasoning or _("(no notes)")),
        )
        self.message_post(body=body)
        if result.status == "escalated":
            self._schedule_escalation_activity(result)

    def _schedule_escalation_activity(self, result):
        """Schedule a to-do activity carrying a structured what-I-tried summary.

        Uses only data the loop already recorded (action_ids + reasoning +
        termination reason). No extra API call — the assistant's last text,
        which the loop captured as ``reasoning``, becomes the human-facing
        recommendation.
        """
        self.ensure_one()
        actions = self.action_ids.sorted("sequence")
        lines = Markup("<p><strong>What the AI tried:</strong></p><ol>")
        for action in actions:
            status_pill = "success" if action.succeeded else "danger"
            detail = action.error_message or "ok"
            lines += Markup(
                "<li><code>%s</code> — <span class='badge text-bg-%s'>%s</span></li>",
            ) % (
                escape(action.tool_name or "?"),
                status_pill,
                escape(detail[:120]),
            )
        lines += Markup("</ol>")
        recommendation = (result.reasoning or "").strip() or _(
            "The agent had no closing recommendation.",
        )
        note = lines + Markup(
            "<p><strong>Termination reason:</strong> %s</p>" "<p><strong>Agent's closing note:</strong> %s</p>",
        ) % (
            escape(result.reason or "-"),
            escape(recommendation),
        )
        assignee = self.team_id.member_ids[:1] if self.team_id else self.env["res.users"]
        self.sudo().activity_schedule(
            act_type_xmlid="mail.mail_activity_data_todo",
            summary=_("AI escalated: review and take over"),
            note=note,
            user_id=assignee.id if assignee else False,
        )

    def action_view_actions(self):
        """Open the AI Actions related to this ticket."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("AI Actions"),
            "res_model": "ai.helpdesk.action",
            "view_mode": "list,form",
            "domain": [("ticket_id", "=", self.id)],
        }

    def action_assign(self):
        """Move an AI-triaged ticket to Assigned after a human picks an owner."""
        for ticket in self:
            ticket._ensure_state("ai_triaged")
            if not ticket.user_id:
                raise UserError(
                    _("Please select an assignee before assigning the ticket."),
                )
        self.write({"state": "assigned"})
        return True

    def action_start_progress(self):
        """Mark assigned tickets as actively being worked."""
        for ticket in self:
            ticket._ensure_state("assigned")
        self.write({"state": "in_progress"})
        return True

    def action_resolve(self):
        """Mark in-progress tickets as resolved by a human."""
        for ticket in self:
            ticket._ensure_state("in_progress")
        self.write({"state": "resolved"})
        return True

    def action_close(self):
        """Close resolved tickets after final human review."""
        for ticket in self:
            ticket._ensure_state("resolved")
        self.write({"state": "closed"})
        return True

    def action_reset_new(self):
        """Reset a ticket and clear AI output so it can be triaged again."""
        self.with_context(ai_skip_correction_log=True).write(
            {
                "state": "new",
                "ai_triaged": False,
                "ai_triage_attempted": False,
                "ai_triage_in_progress": False,
                "ai_triaged_date": False,
                "ai_reasoning": False,
                "ai_suggested_reply": False,
                "ai_confidence": 0.0,
                "ai_review_status": "none",
                "ai_review_note": False,
                "ai_error": False,
                "ai_model": False,
                "ai_input_tokens": 0,
                "ai_output_tokens": 0,
                "ai_total_cost": 0.0,
                "ai_original_category": False,
                "ai_original_priority": False,
                "ai_original_team_id": False,
                "ai_resolution_status": "not_attempted",
                "ai_resolution_in_progress": False,
                "ai_resolution_attempts": 0,
                "ai_resolution_cost": 0.0,
                "ai_resolution_reasoning": False,
                "ai_resolution_reason": False,
                "ai_resolution_start_at": False,
                "ai_resolution_end_at": False,
                "ai_sentiment": "neutral",
                "ai_urgency": "normal",
                "ai_ambiguous": False,
            },
        )
        return True

    @api.model
    def _cron_auto_triage_new_tickets(self, limit=20):
        """Scheduled-action entry point for unattended first-pass triage."""
        tickets = self.search(
            [
                ("state", "=", "new"),
                ("ai_triaged", "=", False),
                ("ai_triage_attempted", "=", False),
                ("ai_triage_in_progress", "=", False),
            ],
            limit=limit,
            order="create_date asc, id asc",
        )
        for ticket in tickets:
            try:
                ticket.action_ai_triage()
            except UserError as exc:
                _logger.warning("Auto-triage skipped ticket %s: %s", ticket.id, exc)
        return True

    def _lock_for_triage(self):
        """Serialize triage attempts so double-clicks cannot overwrite output."""
        self.ensure_one()
        self.env.cr.execute(
            f"SELECT id FROM {self._table} WHERE id = %s FOR UPDATE",
            [self.id],
        )
        if hasattr(self, "invalidate_recordset"):
            self.invalidate_recordset(
                ["state", "ai_triaged", "ai_triage_attempted", "ai_triage_in_progress"],
            )
        else:  # pragma: no cover - compatibility for older ORM branches
            self.invalidate_cache(
                ["state", "ai_triaged", "ai_triage_attempted", "ai_triage_in_progress"],
            )

    def _ensure_can_triage(self):
        """Reject repeated or invalid triage attempts before calling the API."""
        self.ensure_one()
        if self.state != "new":
            raise UserError(_("Only New tickets can be AI-triaged."))
        if self.ai_triaged or self.ai_triage_attempted:
            raise UserError(
                _("This ticket already has an AI triage attempt. Reset it first."),
            )
        if self.ai_triage_in_progress:
            raise UserError(_("AI triage is already running for this ticket."))

    def _ensure_state(self, expected_state):
        """Raise a clear error if a workflow action is clicked out of order."""
        self.ensure_one()
        if self.state != expected_state:
            label = dict(self._fields["state"].selection).get(
                expected_state,
                expected_state,
            )
            raise UserError(
                _("This action is only available from the %s state.") % label,
            )

    def _call_ai_triage_agent(self):
        """Call Anthropic, validate structured output, and return normalized triage data."""
        self.ensure_one()
        self._guard_ai_prerequisites()
        self._guard_daily_budget()

        teams_by_key = self._get_available_team_map()
        team_names = [team.name for team in teams_by_key.values()]
        prompt = self._build_triage_prompt(team_names)
        payload, usage = self._request_triage_payload(prompt, team_names)
        result, errors = self._validate_ai_payload(payload, teams_by_key)

        if errors:
            corrective_prompt = self._build_triage_prompt(
                team_names,
                corrective_errors=errors,
                previous_payload=payload,
            )
            retry_payload, retry_usage = self._request_triage_payload(
                corrective_prompt,
                team_names,
            )
            usage = self._merge_usage(usage, retry_usage)
            result, errors = self._validate_ai_payload(retry_payload, teams_by_key)

        if errors:
            result = self._fallback_triage_result(errors)

        result.update(self._decision_for_confidence(result["confidence"]))
        result["usage"] = usage
        result["model"] = ANTHROPIC_MODEL
        result["estimated_cost"] = self._estimate_cost(
            usage.get("input_tokens", 0),
            usage.get("output_tokens", 0),
            model_id=result["model"],
        )
        return result

    def _build_triage_prompt(
        self,
        team_names,
        corrective_errors=None,
        previous_payload=None,
    ):
        """Build the user prompt; structured output is enforced by Anthropic tools."""
        self.ensure_one()
        redaction_enabled = self._get_bool_param(
            "ai_helpdesk_triage.redact_pii",
            default=True,
        )
        subject = self._redact_pii(self.name) if redaction_enabled else self.name
        description = self._redact_pii(self.description) if redaction_enabled else self.description
        customer = self.partner_id.display_name or _("Unknown")
        customer = self._redact_pii(customer) if redaction_enabled else customer
        redaction_note = _(
            "Emails and phone-like values may be replaced with [REDACTED_EMAIL] "
            "or [REDACTED_PHONE] before this prompt leaves Odoo.",
        )
        team_lines = "\n".join(f"- {name}" for name in team_names)
        prompt = f"""You are a senior helpdesk triage agent.
Analyze the ticket and call the triage_ticket tool exactly once.

Human-in-the-loop rule:
- You only perform first-pass classification and routing.
- A human will review the result before assignment, replies, resolution, or closure.

Ticket subject:
{subject}

Ticket description:
{description}

Customer:
{customer}

Available support teams. suggested_team must exactly match one of these names:
{team_lines}

Allowed categories: technical, billing, general, feature_request.
Allowed priorities: 0 low, 1 medium, 2 high, 3 urgent.
Confidence must be a number from 0.0 to 1.0.
{redaction_note if redaction_enabled else "No PII redaction was applied by Odoo before this call."}
"""
        if corrective_errors:
            prompt += "\nThe previous tool input failed validation. Correct these issues:\n"
            prompt += "\n".join(f"- {error}" for error in corrective_errors)
            prompt += "\nPrevious invalid payload:\n"
            prompt += json.dumps(previous_payload, ensure_ascii=True, indent=2)
        return prompt

    def _guard_ai_prerequisites(self):
        """Ensure the server can safely call Anthropic."""
        if requests is None:
            raise UserError(_("The Python 'requests' library is not installed."))
        api_key = self._get_api_key()
        if not api_key:
            raise UserError(
                _("Configure the Anthropic API key in AI Helpdesk settings."),
            )
        if not self.env["ai.helpdesk.team"].search_count([("active", "=", True)]):
            raise UserError(
                _("Create at least one active AI Helpdesk team before triage."),
            )

    def _guard_daily_budget(self):
        """Stop new API calls once today's configured spend cap is reached.

        The cap is combined across triage and resolution: the README and the
        settings help text both describe it as a single per-day guardrail.
        """
        budget = self._get_float_param("ai_helpdesk_triage.daily_budget_usd", 0.0)
        if budget <= 0:
            return
        today = fields.Date.context_today(self)
        day_start = fields.Datetime.to_string(datetime.combine(today, datetime_time.min))
        triage_spend = sum(
            self.search(
                [("ai_triaged_date", ">=", day_start)],
            ).mapped("ai_total_cost"),
        )
        resolution_spend = sum(
            self.search(
                [("ai_resolution_end_at", ">=", day_start)],
            ).mapped("ai_resolution_cost"),
        )
        spend = triage_spend + resolution_spend
        if spend >= budget:
            raise UserError(
                _(
                    "Daily AI budget reached: $%(spend).2f of $%(budget).2f. "
                    "Raise the budget or wait until tomorrow.",
                )
                % {"spend": spend, "budget": budget},
            )

    def _check_circuit_breaker(self):
        """Refuse resolve when recent actions for this category have failed too often.

        The breaker consults the existing ai.helpdesk.action audit log:
        if the failure ratio for the ticket's category over the configured
        lookback window is above the threshold (and enough samples exist),
        we raise a UserError to stop the autonomous loop and post to the
        ticket chatter so the operator sees why. No new persistence.
        """
        self.ensure_one()
        if not self.category:
            return
        window_seconds = int(
            self._get_float_param(
                "ai_helpdesk_triage.circuit_breaker_window_seconds",
                3600.0,
                minimum=60.0,
            ),
        )
        threshold = self._get_float_param(
            "ai_helpdesk_triage.circuit_breaker_failure_threshold",
            0.7,
            minimum=0.0,
            maximum=1.0,
        )
        min_actions = int(
            self._get_float_param(
                "ai_helpdesk_triage.circuit_breaker_min_actions",
                5.0,
                minimum=1.0,
            ),
        )
        cutoff = fields.Datetime.to_string(
            fields.Datetime.now() - timedelta(seconds=window_seconds),
        )
        recent = (
            self.env["ai.helpdesk.action"]
            .sudo()
            .search(
                [
                    ("create_date", ">=", cutoff),
                    ("ticket_id.category", "=", self.category),
                ],
            )
        )
        total = len(recent)
        if total < min_actions:
            return
        failures = len([a for a in recent if not a.succeeded])
        ratio = failures / total
        if ratio < threshold:
            return
        body = Markup(
            "<p><strong>%s</strong></p>"
            "<p>%d of %d recent actions in category <em>%s</em> failed "
            "(%.0f%% &gt;= %.0f%% threshold). Refusing to run to protect "
            "the downstream systems. Investigate the failing tool(s) before "
            "retrying.</p>",
        ) % (
            escape(_("AI resolution paused by circuit breaker")),
            failures,
            total,
            escape(self.category),
            ratio * 100,
            threshold * 100,
        )
        self.sudo().message_post(body=body, message_type="notification", subtype_xmlid="mail.mt_note")
        raise UserError(
            _(
                "Circuit breaker open for category %(cat)s: %(fail)d of "
                "%(total)d recent actions failed. Try again after the failing "
                "tool is fixed.",
            )
            % {"cat": self.category, "fail": failures, "total": total},
        )

    def _request_triage_payload(self, prompt, team_names):
        """Request tool-use output from Anthropic and parse a payload dict."""
        response_data = self._post_anthropic(
            {
                "model": ANTHROPIC_MODEL,
                "max_tokens": MAX_TOKENS,
                "tools": [self._triage_tool_schema(team_names)],
                "tool_choice": {"type": "tool", "name": "triage_ticket"},
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        usage = response_data.get("usage") or {}
        return self._parse_ai_response(response_data), {
            "input_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
        }

    def _post_anthropic(self, payload):
        """POST to the configured LLM provider (Anthropic or Bedrock).

        The method preserves its historical name for backwards compatibility
        with existing tests that patch ``requests.post`` on this module. Body
        assembly is delegated so both triage and the agent loop share the
        same provider dispatch.
        """
        provider = self._get_provider()
        api_key = self._get_api_key()
        if not api_key:
            raise UserError(
                _("Configure the AI provider credentials in AI Helpdesk settings."),
            )
        url, headers, body = self._build_provider_request(provider, api_key, payload)

        last_error = None
        for attempt in range(MAX_RETRIES):
            try:
                response = requests.post(
                    url,
                    headers=headers,
                    json=body,
                    timeout=REQUEST_TIMEOUT,
                )
            except (
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
            ) as exc:
                last_error = exc
                self._sleep_before_retry(attempt)
                continue
            except requests.exceptions.RequestException as exc:
                raise UserError(
                    _("Could not reach the AI service: %s") % str(exc),
                ) from exc

            if response.status_code in TRANSIENT_STATUS_CODES:
                last_error = UserError(
                    _("AI service returned temporary HTTP %(status)s: %(body)s")
                    % {
                        "status": response.status_code,
                        "body": self._safe_response_text(response),
                    },
                )
                self._sleep_before_retry(attempt)
                continue
            if response.status_code >= 400:
                raise UserError(
                    _("AI service rejected the request with HTTP %(status)s: %(body)s")
                    % {
                        "status": response.status_code,
                        "body": self._safe_response_text(response),
                    },
                )
            try:
                return response.json()
            except ValueError as exc:
                raise UserError(_("AI service returned invalid JSON.")) from exc

        raise UserError(_("AI service did not respond after retries: %s") % last_error)

    def _build_provider_request(self, provider, api_key, payload):
        """Return (url, headers, body) tuple for the active provider."""
        body = dict(payload)
        if provider == "bedrock":
            icp = self.env["ir.config_parameter"].sudo()
            region = (
                icp.get_param(
                    "ai_helpdesk_triage.bedrock_region",
                    "us-east-1",
                )
                or "us-east-1"
            )
            model_id = (
                icp.get_param(
                    "ai_helpdesk_triage.bedrock_model_id",
                    "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
                )
                or "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
            )
            body.pop("model", None)
            body.setdefault("anthropic_version", "bedrock-2023-05-31")
            url = f"https://bedrock-runtime.{region}.amazonaws.com/" f"model/{model_id}/invoke"
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            return url, headers, body

        body.setdefault("model", ANTHROPIC_MODEL)
        return (
            ANTHROPIC_API_URL,
            {
                "x-api-key": api_key,
                "anthropic-version": ANTHROPIC_API_VERSION,
                "content-type": "application/json",
            },
            body,
        )

    def _get_provider(self):
        """Read the active provider from ir.config_parameter."""
        value = self.env["ir.config_parameter"].sudo().get_param("ai_helpdesk_triage.provider", "anthropic")
        return value if value in ("anthropic", "bedrock") else "anthropic"

    def _sleep_before_retry(self, attempt):
        """Back off between transient Anthropic failures."""
        if attempt < MAX_RETRIES - 1:
            time.sleep(BACKOFF_SECONDS * (2**attempt))

    @api.model
    def _triage_tool_schema(self, team_names):
        """Return the Anthropic tool schema for validated structured output."""
        return {
            "name": "triage_ticket",
            "description": (
                "Classify, prioritize, route, draft a support reply, and score "
                "the customer's sentiment/urgency so downstream automation can "
                "pick the right model and safety gates."
            ),
            "input_schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": [key for key, _label in CATEGORY_SELECTION],
                    },
                    "priority": {
                        "type": "string",
                        "enum": [key for key, _label in PRIORITY_SELECTION],
                    },
                    "suggested_team": {"type": "string", "enum": team_names},
                    "reasoning": {"type": "string", "minLength": 1},
                    "suggested_reply": {"type": "string", "minLength": 1},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "sentiment": {
                        "type": "string",
                        "enum": [key for key, _label in SENTIMENT_SELECTION],
                        "description": (
                            "Customer's emotional tone in the ticket. "
                            "'angry' or 'frustrated' should bias toward more "
                            "careful resolution and human escalation."
                        ),
                    },
                    "urgency": {
                        "type": "string",
                        "enum": [key for key, _label in URGENCY_SELECTION],
                        "description": (
                            "How business-urgent the ask is. 'vip' forces "
                            "the most capable model on every resolution turn."
                        ),
                    },
                    "is_ambiguous": {
                        "type": "boolean",
                        "description": (
                            "True when the ticket does not clearly describe "
                            "a single action to take (multiple asks, unclear "
                            "intent, missing information)."
                        ),
                    },
                },
                "required": [
                    "category",
                    "priority",
                    "suggested_team",
                    "reasoning",
                    "suggested_reply",
                    "confidence",
                    "sentiment",
                    "urgency",
                    "is_ambiguous",
                ],
            },
        }

    @api.model
    def _parse_ai_response(self, response_data):
        """Extract tool-use input, with a defensive fenced-JSON fallback."""
        content_blocks = response_data.get("content") or []
        for block in content_blocks:
            if block.get("type") == "tool_use" and block.get("name") == "triage_ticket":
                tool_input = block.get("input")
                if isinstance(tool_input, dict):
                    return tool_input
                raise UserError(_("AI tool input was not a JSON object."))

        text = "".join(block.get("text", "") for block in content_blocks if block.get("type") == "text").strip()
        if not text:
            raise UserError(_("AI service returned no tool input."))
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE).strip()
        text = re.sub(r"\s*```$", "", text).strip()
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if match:
            text = match.group(0)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise UserError(
                _("Could not parse AI JSON fallback: %s") % str(exc),
            ) from exc
        if not isinstance(parsed, dict):
            raise UserError(_("AI JSON fallback was not an object."))
        return parsed

    def _validate_ai_payload(self, payload, teams_by_key):
        """Normalize model output and return validation errors without writing records."""
        errors = []
        category = payload.get("category")
        if category not in dict(CATEGORY_SELECTION):
            errors.append(_("category must be one of the allowed values"))
            category = "general"

        priority = payload.get("priority")
        if isinstance(priority, int):
            priority = str(priority)
        if priority not in dict(PRIORITY_SELECTION):
            errors.append(_("priority must be 0, 1, 2, or 3"))
            priority = "1"

        suggested_team = (payload.get("suggested_team") or "").strip()
        team = teams_by_key.get(suggested_team.casefold())
        if not team:
            errors.append(
                _("suggested_team must exactly match an existing active team"),
            )

        reasoning = (payload.get("reasoning") or "").strip()
        if not reasoning:
            errors.append(_("reasoning is required"))
            reasoning = _("AI did not provide reasoning.")

        suggested_reply = (payload.get("suggested_reply") or "").strip()
        if not suggested_reply:
            errors.append(_("suggested_reply is required"))
            suggested_reply = _(
                "Thank you for contacting support. We are reviewing your request.",
            )

        try:
            confidence = float(payload.get("confidence"))
        except (TypeError, ValueError):
            errors.append(_("confidence must be a number between 0 and 1"))
            confidence = 0.0
        if confidence < 0.0 or confidence > 1.0:
            errors.append(_("confidence must be between 0 and 1"))
            confidence = 0.0

        sentiment = payload.get("sentiment") or "neutral"
        if sentiment not in dict(SENTIMENT_SELECTION):
            errors.append(_("sentiment must be neutral, frustrated, or angry"))
            sentiment = "neutral"

        urgency = payload.get("urgency") or "normal"
        if urgency not in dict(URGENCY_SELECTION):
            errors.append(_("urgency must be low, normal, or vip"))
            urgency = "normal"

        is_ambiguous = payload.get("is_ambiguous")
        if isinstance(is_ambiguous, str):
            is_ambiguous = is_ambiguous.strip().lower() in {"1", "true", "yes"}
        is_ambiguous = bool(is_ambiguous)

        return (
            {
                "category": category,
                "priority": priority,
                "team": team,
                "suggested_team": team.name if team else False,
                "reasoning": reasoning,
                "suggested_reply": suggested_reply,
                "confidence": confidence,
                "sentiment": sentiment,
                "urgency": urgency,
                "is_ambiguous": is_ambiguous,
                "validation_errors": errors,
            },
            errors,
        )

    def _fallback_triage_result(self, errors):
        """Return a safe result when both structured attempts fail validation."""
        return {
            "category": "general",
            "priority": "1",
            "team": self.env["ai.helpdesk.team"],
            "suggested_team": False,
            "reasoning": _(
                "AI output failed validation after retry. Human triage is required. " "Validation errors: %s",
            )
            % "; ".join(str(error) for error in errors),
            "suggested_reply": _(
                "Thank you for contacting support. We are reviewing your request and "
                "will route it to the right specialist.",
            ),
            "confidence": 0.0,
            "sentiment": "neutral",
            "urgency": "normal",
            "is_ambiguous": True,
            "validation_errors": errors,
        }

    def _decision_for_confidence(self, confidence):
        """Map confidence to workflow autonomy and review flags."""
        auto_threshold, review_threshold = self._get_confidence_thresholds()
        if confidence >= auto_threshold:
            return {
                "review_status": "accepted",
                "review_note": _("Confidence met the auto-route threshold."),
                "should_transition": True,
                "should_route": True,
            }
        if confidence >= review_threshold:
            return {
                "review_status": "review_recommended",
                "review_note": _("Review recommended before assigning or replying."),
                "should_transition": True,
                "should_route": True,
            }
        return {
            "review_status": "needs_human",
            "review_note": _(
                "Confidence is below the review threshold. Ticket remains New for human triage.",
            ),
            "should_transition": False,
            "should_route": False,
        }

    def _apply_ai_triage_result(self, result):
        """Write validated AI output according to confidence-gating rules."""
        self.ensure_one()
        values = {
            "category": result["category"],
            "priority": result["priority"],
            "ai_reasoning": result["reasoning"],
            "ai_suggested_reply": result["suggested_reply"],
            "ai_confidence": result["confidence"],
            "ai_review_status": result["review_status"],
            "ai_review_note": result["review_note"],
            "ai_sentiment": result.get("sentiment") or "neutral",
            "ai_urgency": result.get("urgency") or "normal",
            "ai_ambiguous": bool(result.get("is_ambiguous")),
            "ai_error": False,
            "ai_triage_attempted": True,
            "ai_triage_in_progress": False,
            "ai_triaged_date": fields.Datetime.now(),
            "ai_model": result["model"],
            "ai_input_tokens": result["usage"].get("input_tokens", 0),
            "ai_output_tokens": result["usage"].get("output_tokens", 0),
            "ai_total_cost": result["estimated_cost"],
            "ai_original_category": result["category"],
            "ai_original_priority": result["priority"],
        }
        team = result.get("team")
        if result["should_route"] and team:
            values["team_id"] = team.id
            values["ai_original_team_id"] = team.id
        if result["should_transition"]:
            values.update({"state": "ai_triaged", "ai_triaged": True})
        else:
            values.update(
                {"state": "new", "ai_triaged": False, "ai_original_team_id": False},
            )
        self.with_context(ai_triage_write=True, ai_skip_correction_log=True).write(
            values,
        )

    def _post_ai_triage_message(self, result):
        """Write an auditable chatter summary for the AI decision."""
        self.ensure_one()
        category_label = self._selection_label("category", result["category"])
        priority_label = self._selection_label("priority", result["priority"])
        team_name = result["team"].display_name if result.get("team") else _("Not routed")
        title = _("AI triage complete") if result["should_transition"] else _("AI triage needs human review")
        body = Markup(
            "<p><strong>%s</strong></p>"
            "<ul>"
            "<li>Category: %s</li>"
            "<li>Priority: %s</li>"
            "<li>Suggested team: %s</li>"
            "<li>Confidence: %.0f%%</li>"
            "<li>Review status: %s</li>"
            "</ul>"
            "<p><strong>Reasoning:</strong> %s</p>",
        ) % (
            escape(title),
            escape(category_label),
            escape(priority_label),
            escape(team_name),
            result["confidence"] * 100,
            escape(self._selection_label("ai_review_status", result["review_status"])),
            escape(result["reasoning"]),
        )
        if result.get("review_note"):
            body += Markup("<p><strong>Review note:</strong> %s</p>") % escape(
                result["review_note"],
            )
        self.message_post(body=body)

    def _prepare_correction_values(self, vals):
        """Build correction rows for user edits to AI-owned fields."""
        if self.env.context.get("ai_skip_correction_log") or self.env.context.get(
            "ai_triage_write",
        ):
            return []
        tracked_fields = sorted(CORRECTION_FIELDS.intersection(vals))
        if not tracked_fields:
            return []

        correction_values = []
        for ticket in self:
            if not ticket.ai_triaged:
                continue
            for field_name in tracked_fields:
                old_raw, new_raw = ticket._raw_correction_values(
                    field_name,
                    vals[field_name],
                )
                if old_raw == new_raw:
                    continue
                correction_values.append(
                    {
                        "ticket_id": ticket.id,
                        "user_id": self.env.user.id,
                        "field_name": field_name,
                        "ai_value": ticket._format_correction_value(
                            field_name,
                            old_raw,
                        ),
                        "human_value": ticket._format_correction_value(
                            field_name,
                            new_raw,
                        ),
                        "ai_confidence": ticket.ai_confidence,
                        "ticket_name": ticket.name,
                        "ticket_description": ticket.description,
                        "ai_reasoning": ticket.ai_reasoning,
                    },
                )
        return correction_values

    def _raw_correction_values(self, field_name, new_value):
        """Return comparable old/new values for a tracked correction field."""
        self.ensure_one()
        if field_name == "team_id":
            return self.team_id.id or False, new_value or False
        return self[field_name] or False, new_value or False

    def _format_correction_value(self, field_name, raw_value):
        """Format correction values as labels suitable for datasets."""
        self.ensure_one()
        if not raw_value:
            return _("None")
        if field_name == "team_id":
            team = self.env["ai.helpdesk.team"].browse(raw_value)
            return team.display_name if team.exists() else str(raw_value)
        return self._selection_label(field_name, raw_value)

    def _selection_label(self, field_name, value):
        """Return the display label for a selection value."""
        selection = dict(self._fields[field_name].selection)
        return selection.get(value, value or _("None"))

    def _get_available_team_map(self):
        """Return active teams keyed case-insensitively by name."""
        teams = self.env["ai.helpdesk.team"].search(
            [("active", "=", True)],
            order="name",
        )
        return {team.name.casefold(): team for team in teams}

    def _get_confidence_thresholds(self):
        """Read and sanitize confidence thresholds from system settings."""
        review_threshold = self._get_float_param(
            "ai_helpdesk_triage.review_confidence_threshold",
            0.5,
            minimum=0.0,
            maximum=1.0,
        )
        auto_threshold = self._get_float_param(
            "ai_helpdesk_triage.auto_route_confidence_threshold",
            0.85,
            minimum=0.0,
            maximum=1.0,
        )
        if auto_threshold < review_threshold:
            auto_threshold = review_threshold
        return auto_threshold, review_threshold

    def _get_api_key(self):
        """Read the credential for the active provider without logging it."""
        icp = self.env["ir.config_parameter"].sudo()
        provider = self._get_provider()
        if provider == "bedrock":
            return icp.get_param("ai_helpdesk_triage.bedrock_api_key")
        return icp.get_param("ai_helpdesk_triage.anthropic_api_key")

    def _get_float_param(self, key, default, minimum=None, maximum=None):
        """Read a float config parameter with defensive defaults."""
        raw_value = self.env["ir.config_parameter"].sudo().get_param(key, default)
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            value = float(default)
        if minimum is not None:
            value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return value

    def _get_bool_param(self, key, default=False):
        """Read a boolean config parameter consistently from ir.config_parameter."""
        raw_value = self.env["ir.config_parameter"].sudo().get_param(key, default)
        if isinstance(raw_value, bool):
            return raw_value
        return str(raw_value).lower() in {"1", "true", "yes", "on"}

    @api.model
    def _merge_usage(self, first, second):
        """Combine Anthropic usage from the initial and corrective attempts."""
        return {
            "input_tokens": int(first.get("input_tokens") or 0) + int(second.get("input_tokens") or 0),
            "output_tokens": int(first.get("output_tokens") or 0) + int(second.get("output_tokens") or 0),
        }

    @api.model
    def _estimate_cost(self, input_tokens, output_tokens, model_id=None):
        """Estimate ticket-level USD cost using the shared per-model tariff."""
        return anthropic_client.estimate_cost(
            input_tokens,
            output_tokens,
            model_id or ANTHROPIC_MODEL,
        )

    @api.model
    def _safe_response_text(self, response):
        """Return a bounded error body that is safe for users and chatter."""
        text = getattr(response, "text", "") or ""
        return text.strip()[:500]

    @api.model
    def _redact_pii(self, text):
        """Redact obvious email addresses and phone-like values before API calls."""
        if not text:
            return text
        text = EMAIL_RE.sub("[REDACTED_EMAIL]", text)
        return PHONE_RE.sub("[REDACTED_PHONE]", text)
