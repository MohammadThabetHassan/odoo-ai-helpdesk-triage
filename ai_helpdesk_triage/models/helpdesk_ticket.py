"""Ticket workflow and Anthropic triage integration."""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from datetime import time as datetime_time

from markupsafe import Markup, escape

from odoo import _, api, fields, models
from odoo.exceptions import UserError

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
INPUT_COST_PER_MILLION = 3.00
OUTPUT_COST_PER_MILLION = 15.00

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

    color = fields.Integer()

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
        description = (
            self._redact_pii(self.description)
            if redaction_enabled
            else self.description
        )
        customer = self.partner_id.display_name or _("Unknown")
        customer = self._redact_pii(customer) if redaction_enabled else customer
        redaction_note = _(
            "Emails and phone-like values may be replaced with [REDACTED_EMAIL] "
            "or [REDACTED_PHONE] before this prompt leaves Odoo.",
        )
        team_lines = "\n".join(f"- {name}" for name in team_names)
        prompt = f"""You are a senior helpdesk triage agent. Analyze the ticket and call the triage_ticket tool exactly once.

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
            prompt += (
                "\nThe previous tool input failed validation. Correct these issues:\n"
            )
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
        """Stop new API calls once today's configured spend cap is reached."""
        budget = self._get_float_param("ai_helpdesk_triage.daily_budget_usd", 0.0)
        if budget <= 0:
            return
        today = fields.Date.context_today(self)
        day_start = datetime.combine(today, datetime_time.min)
        spend = sum(
            self.search(
                [("ai_triaged_date", ">=", fields.Datetime.to_string(day_start))],
            ).mapped("ai_total_cost"),
        )
        if spend >= budget:
            raise UserError(
                _(
                    "Daily AI triage budget reached: $%(spend).2f of $%(budget).2f. "
                    "Raise the budget or wait until tomorrow.",
                )
                % {"spend": spend, "budget": budget},
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
        """POST to Anthropic with timeouts and exponential backoff."""
        api_key = self._get_api_key()
        last_error = None
        for attempt in range(MAX_RETRIES):
            try:
                response = requests.post(
                    ANTHROPIC_API_URL,
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": ANTHROPIC_API_VERSION,
                        "content-type": "application/json",
                    },
                    json=payload,
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

    def _sleep_before_retry(self, attempt):
        """Back off between transient Anthropic failures."""
        if attempt < MAX_RETRIES - 1:
            time.sleep(BACKOFF_SECONDS * (2**attempt))

    @api.model
    def _triage_tool_schema(self, team_names):
        """Return the Anthropic tool schema for validated structured output."""
        return {
            "name": "triage_ticket",
            "description": "Classify, prioritize, route, and draft a support reply.",
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
                },
                "required": [
                    "category",
                    "priority",
                    "suggested_team",
                    "reasoning",
                    "suggested_reply",
                    "confidence",
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

        text = "".join(
            block.get("text", "")
            for block in content_blocks
            if block.get("type") == "text"
        ).strip()
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

        return (
            {
                "category": category,
                "priority": priority,
                "team": team,
                "suggested_team": team.name if team else False,
                "reasoning": reasoning,
                "suggested_reply": suggested_reply,
                "confidence": confidence,
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
                "AI output failed validation after retry. Human triage is required. "
                "Validation errors: %s",
            )
            % "; ".join(str(error) for error in errors),
            "suggested_reply": _(
                "Thank you for contacting support. We are reviewing your request and "
                "will route it to the right specialist.",
            ),
            "confidence": 0.0,
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
        team_name = (
            result["team"].display_name if result.get("team") else _("Not routed")
        )
        title = (
            _("AI triage complete")
            if result["should_transition"]
            else _("AI triage needs human review")
        )
        body = Markup(
            "<p><strong>%s</strong></p>"
            "<ul>"
            "<li>Category: %s</li>"
            "<li>Priority: %s</li>"
            "<li>Suggested team: %s</li>"
            "<li>Confidence: %.0f%%</li>"
            "<li>Review status: %s</li>"
            "<li>Tokens: %s input / %s output</li>"
            "<li>Estimated cost: $%.6f</li>"
            "</ul>"
            "<p><strong>Reasoning:</strong> %s</p>",
        ) % (
            escape(title),
            escape(category_label),
            escape(priority_label),
            escape(team_name),
            result["confidence"] * 100,
            escape(self._selection_label("ai_review_status", result["review_status"])),
            result["usage"].get("input_tokens", 0),
            result["usage"].get("output_tokens", 0),
            result["estimated_cost"],
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
        """Read the Anthropic API key without logging or exposing it."""
        return (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("ai_helpdesk_triage.anthropic_api_key")
        )

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
            "input_tokens": int(first.get("input_tokens") or 0)
            + int(second.get("input_tokens") or 0),
            "output_tokens": int(first.get("output_tokens") or 0)
            + int(second.get("output_tokens") or 0),
        }

    @api.model
    def _estimate_cost(self, input_tokens, output_tokens):
        """Estimate ticket-level USD cost from Anthropic token counts."""
        input_cost = (input_tokens / 1_000_000) * INPUT_COST_PER_MILLION
        output_cost = (output_tokens / 1_000_000) * OUTPUT_COST_PER_MILLION
        return round(input_cost + output_cost, 6)

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
