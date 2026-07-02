"""AI response parsing and reliability tests."""

import json
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


class FakeResponse:
    """Small requests.Response stand-in for mocked Anthropic calls."""

    def __init__(self, payload, status_code=200, text="OK"):
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):
        return self._payload


@tagged("post_install", "-at_install")
class TestAiParsing(TransactionCase):
    """Cover structured output parsing, validation, and reliability branches."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.team = cls.env["ai.helpdesk.team"].create({"name": "Billing Support"})
        cls.ticket = cls.env["ai.helpdesk.ticket"].create(
            {
                "name": "Refund request",
                "description": "Customer asks for a refund after duplicate billing.",
            },
        )
        cls.env["ir.config_parameter"].sudo().set_param(
            "ai_helpdesk_triage.anthropic_api_key",
            "test-key",
        )

    def _anthropic_payload(self, tool_input, input_tokens=100, output_tokens=40):
        return {
            "content": [
                {"type": "tool_use", "name": "triage_ticket", "input": tool_input},
            ],
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        }

    def _valid_input(self, confidence=0.91):
        return {
            "category": "billing",
            "priority": "2",
            "suggested_team": self.team.name,
            "reasoning": "The customer reports duplicate billing.",
            "suggested_reply": "We are checking the duplicate charge and will update you shortly.",
            "confidence": confidence,
        }

    def test_tool_use_payload_is_normalized(self):
        """Anthropic tool-use input becomes normalized ticket triage data."""
        response = FakeResponse(self._anthropic_payload(self._valid_input()))
        with patch(
            "odoo.addons.ai_helpdesk_triage.models.helpdesk_ticket.requests.post",
            return_value=response,
        ) as mocked_post:
            result = self.ticket._call_ai_triage_agent()

        self.assertEqual(mocked_post.call_count, 1)
        self.assertEqual(result["category"], "billing")
        self.assertEqual(result["team"], self.team)
        self.assertTrue(result["should_transition"])
        self.assertEqual(result["review_status"], "accepted")
        self.assertEqual(result["usage"], {"input_tokens": 100, "output_tokens": 40})
        self.assertGreater(result["estimated_cost"], 0)

    def test_fenced_json_fallback_parser(self):
        """Older text-only JSON responses are parsed defensively."""
        payload = self._valid_input(confidence=0.7)
        response_data = {
            "content": [
                {"type": "text", "text": "```json\n" + json.dumps(payload) + "\n```"},
            ],
        }
        parsed = self.ticket._parse_ai_response(response_data)
        self.assertEqual(parsed, payload)

    def test_invalid_payload_retries_once_then_succeeds(self):
        """Validation errors trigger exactly one corrective prompt before writing."""
        invalid = {
            "category": "other",
            "priority": "7",
            "suggested_team": "Missing Team",
            "reasoning": "Bad enum values.",
            "suggested_reply": "Reply.",
            "confidence": 0.9,
        }
        responses = [
            FakeResponse(self._anthropic_payload(invalid, 10, 5)),
            FakeResponse(self._anthropic_payload(self._valid_input(), 20, 6)),
        ]
        with patch(
            "odoo.addons.ai_helpdesk_triage.models.helpdesk_ticket.requests.post",
            side_effect=responses,
        ) as mocked_post:
            result = self.ticket._call_ai_triage_agent()

        self.assertEqual(mocked_post.call_count, 2)
        self.assertEqual(result["category"], "billing")
        self.assertEqual(result["usage"], {"input_tokens": 30, "output_tokens": 11})

    def test_invalid_payload_falls_back_to_human_triage(self):
        """Two invalid tool payloads fall back to general and needs-human."""
        invalid = {
            "category": "bad",
            "priority": "9",
            "suggested_team": "Missing Team",
            "reasoning": "No valid route.",
            "suggested_reply": "Reply.",
            "confidence": 2,
        }
        with patch(
            "odoo.addons.ai_helpdesk_triage.models.helpdesk_ticket.requests.post",
            return_value=FakeResponse(self._anthropic_payload(invalid)),
        ):
            result = self.ticket._call_ai_triage_agent()

        self.assertEqual(result["category"], "general")
        self.assertEqual(result["review_status"], "needs_human")
        self.assertFalse(result["should_transition"])

    def test_low_confidence_branch(self):
        """Confidence below the review threshold remains New for a human."""
        response = FakeResponse(
            self._anthropic_payload(self._valid_input(confidence=0.2)),
        )
        with patch(
            "odoo.addons.ai_helpdesk_triage.models.helpdesk_ticket.requests.post",
            return_value=response,
        ):
            result = self.ticket._call_ai_triage_agent()

        self.assertEqual(result["review_status"], "needs_human")
        self.assertFalse(result["should_route"])
        self.assertFalse(result["should_transition"])

    def test_transient_http_status_retries(self):
        """Transient API failures are retried with backoff."""
        responses = [
            FakeResponse({}, status_code=500, text="temporary"),
            FakeResponse(self._anthropic_payload(self._valid_input())),
        ]
        with (
            patch(
                "odoo.addons.ai_helpdesk_triage.models.helpdesk_ticket.requests.post",
                side_effect=responses,
            ) as mocked_post,
            patch(
                "odoo.addons.ai_helpdesk_triage.models.helpdesk_ticket.time.sleep",
            ) as mocked_sleep,
        ):
            result = self.ticket._call_ai_triage_agent()

        self.assertEqual(mocked_post.call_count, 2)
        mocked_sleep.assert_called_once()
        self.assertEqual(result["category"], "billing")

    def test_daily_budget_guard_blocks_call(self):
        """Budget enforcement happens before any network call."""
        self.env["ir.config_parameter"].sudo().set_param(
            "ai_helpdesk_triage.daily_budget_usd",
            "0.01",
        )
        self.env["ai.helpdesk.ticket"].create(
            {
                "name": "Already spent",
                "description": "Existing usage",
                "ai_triaged_date": fields.Datetime.now(),
                "ai_total_cost": 0.02,
            },
        )
        try:
            with (
                patch(
                    "odoo.addons.ai_helpdesk_triage.models.helpdesk_ticket.requests.post",
                ) as mocked_post,
                self.assertRaises(UserError),
            ):
                self.ticket._call_ai_triage_agent()
            mocked_post.assert_not_called()
        finally:
            self.env["ir.config_parameter"].sudo().set_param(
                "ai_helpdesk_triage.daily_budget_usd",
                "0",
            )

    def test_prompt_redacts_obvious_pii(self):
        """Configured redaction removes obvious email and phone-like values."""
        self.env["ir.config_parameter"].sudo().set_param(
            "ai_helpdesk_triage.redact_pii",
            "True",
        )
        ticket = self.env["ai.helpdesk.ticket"].create(
            {
                "name": "Call me at +971 50 123 4567",
                "description": "My email is person@example.com and login is broken.",
            },
        )
        prompt = ticket._build_triage_prompt([self.team.name])
        self.assertIn("[REDACTED_EMAIL]", prompt)
        self.assertIn("[REDACTED_PHONE]", prompt)
        self.assertNotIn("person@example.com", prompt)
