"""Unit tests for the find_similar_tickets retrieval tool."""

from odoo.addons.ai_helpdesk_triage.models.tools.retrieval_tools import (
    find_similar_tickets,
)
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestFindSimilarTickets(TransactionCase):
    """Postgres FTS retrieval returns lexically-close resolved tickets."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.team = cls.env["ai.helpdesk.team"].create(
            {"name": "Billing Support", "description": "Billing and refunds"},
        )
        Ticket = cls.env["ai.helpdesk.ticket"].sudo()

        # Seed three resolved tickets with distinct vocabularies.
        cls.resolved_password = Ticket.create(
            {
                "name": "Cannot reset my password",
                "description": "The password reset link expired before I clicked it.",
                "category": "technical",
                "team_id": cls.team.id,
                "ai_triaged": True,
                "state": "resolved",
                "ai_resolution_status": "resolved",
                "ai_resolution_reasoning": "Sent a fresh password reset link.",
            },
        )
        cls.resolved_invoice = Ticket.create(
            {
                "name": "Missing invoice for March",
                "description": ("My finance team never received invoice INV/2026/00042. " "Please resend the PDF."),
                "category": "billing",
                "team_id": cls.team.id,
                "ai_triaged": True,
                "state": "resolved",
                "ai_resolution_status": "resolved",
                "ai_resolution_reasoning": "Resent the invoice PDF via email template.",
            },
        )
        cls.resolved_delivery = Ticket.create(
            {
                "name": "Delivery delayed by a week",
                "description": "The courier said my package would arrive by Monday.",
                "category": "general",
                "team_id": cls.team.id,
                "ai_triaged": True,
                "state": "resolved",
                "ai_resolution_status": "resolved",
                "ai_resolution_reasoning": "Refunded shipping fee and expedited.",
            },
        )
        # A ticket in a non-resolved state should never be returned.
        cls.open_password = Ticket.create(
            {
                "name": "Another password reset request",
                "description": "Reset my account password please.",
                "category": "technical",
                "team_id": cls.team.id,
                "state": "new",
                "ai_resolution_status": "not_attempted",
            },
        )
        # Attach a tool history to the invoice ticket so we can assert on
        # tool_sequence in the query result.
        Action = cls.env["ai.helpdesk.action"].sudo()
        Action.create(
            {
                "ticket_id": cls.resolved_invoice.id,
                "sequence": 1,
                "tool_name": "lookup_invoice",
                "tool_input": "{}",
                "tool_result": '{"ok": true}',
                "succeeded": True,
            },
        )
        Action.create(
            {
                "ticket_id": cls.resolved_invoice.id,
                "sequence": 2,
                "tool_name": "resend_invoice_pdf",
                "tool_input": "{}",
                "tool_result": '{"ok": true}',
                "succeeded": True,
            },
        )

    def test_returns_top_ranked_resolved_tickets(self):
        """The password-related query matches the resolved password ticket only."""
        current = self.env["ai.helpdesk.ticket"].create(
            {
                "name": "Password reset not working",
                "description": "I need to reset my password but the link never arrives.",
                "team_id": self.team.id,
            },
        )
        result = find_similar_tickets(
            self.env,
            current,
            query="password reset link",
            limit=5,
        )
        self.assertTrue(result["ok"])
        subjects = [t["subject"] for t in result["data"]["tickets"]]
        self.assertIn("Cannot reset my password", subjects)
        # Non-resolved tickets must not appear even when they lexically match.
        self.assertNotIn("Another password reset request", subjects)

    def test_excludes_current_ticket(self):
        """The tool never returns the current ticket itself."""
        result = find_similar_tickets(
            self.env,
            self.resolved_invoice,
            query="invoice PDF",
            limit=5,
        )
        self.assertTrue(result["ok"])
        ids = [t["id"] for t in result["data"]["tickets"]]
        self.assertNotIn(self.resolved_invoice.id, ids)

    def test_returns_tool_sequence_and_notes(self):
        """Hits carry the tool sequence and resolution notes for few-shot use."""
        current = self.env["ai.helpdesk.ticket"].create(
            {
                "name": "Please resend my invoice",
                "description": "I did not receive the invoice PDF for last month.",
                "team_id": self.team.id,
            },
        )
        result = find_similar_tickets(
            self.env,
            current,
            query="invoice PDF resend",
        )
        self.assertTrue(result["ok"])
        tickets = result["data"]["tickets"]
        self.assertTrue(tickets, "Expected at least one lexical match")
        invoice_hit = next(
            (t for t in tickets if t["subject"] == "Missing invoice for March"),
            None,
        )
        self.assertIsNotNone(invoice_hit)
        self.assertEqual(
            invoice_hit["tool_sequence"],
            ["lookup_invoice", "resend_invoice_pdf"],
        )
        self.assertIn("Resent the invoice PDF", invoice_hit["resolution_notes"])

    def test_empty_result_when_nothing_matches(self):
        """Unrelated query returns an empty tickets list, not an error."""
        current = self.env["ai.helpdesk.ticket"].create(
            {
                "name": "Weather report",
                "description": "This has nothing to do with the seeded tickets.",
                "team_id": self.team.id,
            },
        )
        result = find_similar_tickets(
            self.env,
            current,
            query="quantum entanglement calibration",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["tickets"], [])

    def test_pii_is_redacted_across_customer_boundary(self):
        """Subjects and resolution notes from other customers must be scrubbed."""
        # Seed a resolved ticket whose subject and notes both leak PII of
        # customer A.
        Ticket = self.env["ai.helpdesk.ticket"].sudo()
        leaky = Ticket.create(
            {
                "name": "Refund john.smith@acme.com duplicate charge",
                "description": (
                    "Handled the refund for the duplicate charge on "
                    "john.smith@acme.com."
                ),
                "category": "billing",
                "team_id": self.team.id,
                "ai_triaged": True,
                "state": "resolved",
                "ai_resolution_status": "resolved",
                "ai_resolution_reasoning": (
                    "Refunded john.smith@acme.com — confirmed at +1 555 111 2222."
                ),
            },
        )
        # Customer B files a similar-sounding ticket.
        current = self.env["ai.helpdesk.ticket"].create(
            {
                "name": "Refund a duplicate charge",
                "description": "I was double-billed.",
                "team_id": self.team.id,
            },
        )
        # Confirm redaction is on by default and verify the tool honors it.
        self.env["ir.config_parameter"].sudo().set_param(
            "ai_helpdesk_triage.redact_pii",
            "True",
        )
        result = find_similar_tickets(
            self.env,
            current,
            query="refund duplicate charge",
        )
        hit = next(t for t in result["data"]["tickets"] if t["id"] == leaky.id)
        self.assertNotIn("john.smith@acme.com", hit["subject"])
        self.assertNotIn("john.smith@acme.com", hit["resolution_notes"])
        self.assertNotIn("+1 555 111 2222", hit["resolution_notes"])
        self.assertIn("[REDACTED_EMAIL]", hit["subject"])

    def test_tool_sequence_filters_reflection_check_meta_rows(self):
        """Reflection audit rows must not pollute the few-shot tool sequence."""
        Action = self.env["ai.helpdesk.action"].sudo()
        # Append a reflection_check meta-row to the invoice ticket's history.
        Action.create(
            {
                "ticket_id": self.resolved_invoice.id,
                "sequence": 3,
                "tool_name": "reflection_check",
                "tool_input": '{"asked": "did last action solve the ask"}',
                "tool_result": '{"ok": true}',
                "succeeded": True,
            },
        )
        current = self.env["ai.helpdesk.ticket"].create(
            {
                "name": "Please resend my invoice",
                "description": "I did not receive the invoice PDF for last month.",
                "team_id": self.team.id,
            },
        )
        result = find_similar_tickets(
            self.env,
            current,
            query="invoice PDF resend",
        )
        hit = next(t for t in result["data"]["tickets"] if t["subject"].startswith("Missing invoice"))
        self.assertNotIn("reflection_check", hit["tool_sequence"])
        self.assertEqual(
            hit["tool_sequence"],
            ["lookup_invoice", "resend_invoice_pdf"],
        )
