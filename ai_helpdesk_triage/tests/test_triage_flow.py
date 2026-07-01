"""Workflow and idempotency tests for AI Helpdesk tickets."""

from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestTriageFlow(TransactionCase):
    """Cover human-in-the-loop state transitions and triage guards."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.team = cls.env["ai.helpdesk.team"].create({"name": "Technical Support"})
        cls.ticket = cls.env["ai.helpdesk.ticket"].create(
            {
                "name": "Login outage",
                "description": "Users cannot log in after SSO callback.",
            },
        )

    def _result(self, confidence=0.92, should_transition=True, should_route=True):
        review_status = "accepted" if should_transition else "needs_human"
        return {
            "category": "technical",
            "priority": "3",
            "team": self.team if should_route else self.env["ai.helpdesk.team"],
            "suggested_team": self.team.name if should_route else False,
            "reasoning": "Authentication errors affect multiple users.",
            "suggested_reply": "We are investigating the login issue.",
            "confidence": confidence,
            "review_status": review_status,
            "review_note": "Confidence gate test.",
            "should_transition": should_transition,
            "should_route": should_route,
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "model": "claude-sonnet-4-6",
            "estimated_cost": 0.00105,
        }

    def test_full_state_machine_requires_human_clicks(self):
        """AI only performs New -> AI Triaged; later moves require explicit actions."""
        with patch(
            "odoo.addons.ai_helpdesk_triage.models.helpdesk_ticket.HelpdeskTicket._call_ai_triage_agent",
            return_value=self._result(),
        ):
            self.ticket.action_ai_triage()

        self.assertEqual(self.ticket.state, "ai_triaged")
        self.assertTrue(self.ticket.ai_triaged)
        self.assertEqual(self.ticket.team_id, self.team)
        self.assertEqual(self.ticket.priority, "3")

        with self.assertRaises(UserError):
            self.ticket.action_assign()

        self.ticket.user_id = self.env.ref("base.user_admin")
        self.ticket.action_assign()
        self.assertEqual(self.ticket.state, "assigned")
        self.ticket.action_start_progress()
        self.assertEqual(self.ticket.state, "in_progress")
        self.ticket.action_resolve()
        self.assertEqual(self.ticket.state, "resolved")
        self.ticket.action_close()
        self.assertEqual(self.ticket.state, "closed")

    def test_idempotency_blocks_second_triage(self):
        """A ticket with an AI attempt cannot be triaged again without reset."""
        ticket = self.env["ai.helpdesk.ticket"].create(
            {"name": "Invoice issue", "description": "Invoice amount is wrong."},
        )
        with patch(
            "odoo.addons.ai_helpdesk_triage.models.helpdesk_ticket.HelpdeskTicket._call_ai_triage_agent",
            return_value=self._result(),
        ):
            ticket.action_ai_triage()

        with self.assertRaises(UserError):
            ticket.action_ai_triage()

        ticket.action_reset_new()
        self.assertEqual(ticket.state, "new")
        self.assertFalse(ticket.ai_triage_attempted)

    def test_low_confidence_stays_new_with_banner_state(self):
        """Low confidence stores analysis but leaves the ticket in New."""
        ticket = self.env["ai.helpdesk.ticket"].create(
            {
                "name": "Ambiguous request",
                "description": "Please help with my account.",
            },
        )
        with patch(
            "odoo.addons.ai_helpdesk_triage.models.helpdesk_ticket.HelpdeskTicket._call_ai_triage_agent",
            return_value=self._result(
                0.31,
                should_transition=False,
                should_route=False,
            ),
        ):
            action = ticket.action_ai_triage()

        self.assertEqual(ticket.state, "new")
        self.assertFalse(ticket.ai_triaged)
        self.assertTrue(ticket.ai_triage_attempted)
        self.assertEqual(ticket.ai_review_status, "needs_human")
        self.assertEqual(action["tag"], "display_notification")

    def test_human_override_creates_correction(self):
        """Changing AI-owned fields after triage captures a labeled correction row."""
        ticket = self.env["ai.helpdesk.ticket"].create(
            {"name": "Feature idea", "description": "Add an export button."},
        )
        with patch(
            "odoo.addons.ai_helpdesk_triage.models.helpdesk_ticket.HelpdeskTicket._call_ai_triage_agent",
            return_value=self._result(),
        ):
            ticket.action_ai_triage()

        ticket.write({"category": "feature_request"})
        correction = self.env["ai.helpdesk.correction"].search(
            [("ticket_id", "=", ticket.id), ("field_name", "=", "category")],
        )
        self.assertEqual(len(correction), 1)
        self.assertEqual(correction.ai_value, "Technical")
        self.assertEqual(correction.human_value, "Feature Request")
