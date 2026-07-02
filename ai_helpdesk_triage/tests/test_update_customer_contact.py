"""Unit tests for the update_customer_contact write tool."""

from odoo.addons.ai_helpdesk_triage.models.tools.write_tools import (
    update_customer_contact,
)
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestUpdateCustomerContact(TransactionCase):
    """Direct callable tests — the agent-loop wiring is covered elsewhere."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner = cls.env["res.partner"].create(
            {
                "name": "Jane Demo",
                "email": "old.address@example.com",
                "phone": "+1 555 111 2222",
            },
        )
        cls.team = cls.env["ai.helpdesk.team"].create(
            {"name": "Support", "description": "General"},
        )

    def _ticket(self, with_partner=True):
        return self.env["ai.helpdesk.ticket"].create(
            {
                "name": "Please update my email",
                "description": "I want to change my email address.",
                "partner_id": self.partner.id if with_partner else False,
                "team_id": self.team.id,
            },
        )

    def test_no_partner_returns_error(self):
        """The tool refuses to run without a linked partner."""
        ticket = self._ticket(with_partner=False)
        result = update_customer_contact(self.env, ticket, email="new@example.com")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "no_partner")

    def test_no_changes_when_values_match(self):
        """Passing the current stored values is a no-op success."""
        ticket = self._ticket()
        result = update_customer_contact(
            self.env,
            ticket,
            email="old.address@example.com",
            phone="+1 555 111 2222",
        )
        self.assertTrue(result["ok"])
        self.assertFalse(result["data"]["updated"])
        self.assertEqual(result["data"]["reason"], "no_changes")

    def test_updates_email_and_phone(self):
        """Both fields change together when new values are supplied."""
        ticket = self._ticket()
        result = update_customer_contact(
            self.env,
            ticket,
            email="new@example.com",
            phone="+1 555 999 8888",
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["data"]["updated"])
        self.partner.invalidate_recordset(["email", "phone"])
        self.assertEqual(self.partner.email, "new@example.com")
        self.assertEqual(self.partner.phone, "+1 555 999 8888")

    def test_partial_update_email_only(self):
        """Passing only email leaves phone untouched."""
        ticket = self._ticket()
        old_phone = self.partner.phone
        result = update_customer_contact(
            self.env,
            ticket,
            email="only-email@example.com",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["changes"], {"email": "only-email@example.com"})
        self.partner.invalidate_recordset(["phone"])
        self.assertEqual(self.partner.phone, old_phone)

    def test_posts_audit_note_to_ticket(self):
        """A successful change leaves an old → new note in the chatter."""
        ticket = self._ticket()
        prior_message_count = len(ticket.message_ids)
        update_customer_contact(self.env, ticket, email="audit@example.com")
        self.assertGreater(len(ticket.message_ids), prior_message_count)
        latest = ticket.message_ids.sorted("id", reverse=True)[0]
        body = latest.body or ""
        self.assertIn("email", body)
        self.assertIn("audit@example.com", body)
