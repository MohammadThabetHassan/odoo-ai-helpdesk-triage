"""Security tests for AI Helpdesk Triage."""

from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestSecurity(TransactionCase):
    """Verify model access rights match the human-review roles."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.group_user = cls.env.ref("ai_helpdesk_triage.group_helpdesk_user")
        cls.group_manager = cls.env.ref("ai_helpdesk_triage.group_helpdesk_manager")
        cls.internal_group = cls.env.ref("base.group_user")
        cls.helpdesk_user = cls.env["res.users"].create(
            {
                "name": "Helpdesk User",
                "login": "helpdesk.user@example.com",
                "email": "helpdesk.user@example.com",
                "group_ids": [(6, 0, [cls.internal_group.id, cls.group_user.id])],
            },
        )
        cls.helpdesk_manager = cls.env["res.users"].create(
            {
                "name": "Helpdesk Manager",
                "login": "helpdesk.manager@example.com",
                "email": "helpdesk.manager@example.com",
                "group_ids": [(6, 0, [cls.internal_group.id, cls.group_manager.id])],
            },
        )

    def test_manager_implies_user_and_admin_is_manager(self):
        """Manager group inherits user group and base admin is added by data."""
        self.assertIn(self.group_user, self.group_manager.implied_ids)
        self.assertIn(self.group_manager, self.env.ref("base.user_admin").group_ids)

    def test_user_can_create_write_but_not_delete_ticket(self):
        """Helpdesk users can work tickets but cannot delete records."""
        Ticket = self.env["ai.helpdesk.ticket"].with_user(self.helpdesk_user)
        ticket = Ticket.create(
            {
                "name": "User-created ticket",
                "description": "A user can create and update this ticket.",
            },
        )
        ticket.write({"priority": "2"})
        with self.assertRaises(AccessError):
            ticket.unlink()

    def test_manager_can_delete_ticket(self):
        """Managers have delete rights for cleanup and configuration ownership."""
        ticket = self.env["ai.helpdesk.ticket"].create(
            {"name": "Manager cleanup", "description": "Can be deleted by manager."},
        )
        ticket.with_user(self.helpdesk_manager).unlink()
        self.assertFalse(ticket.exists())

    def test_public_user_has_no_ticket_access(self):
        """Users outside AI Helpdesk groups cannot read the custom models."""
        with self.assertRaises(AccessError):
            self.env["ai.helpdesk.ticket"].with_user(
                self.env.ref("base.public_user"),
            ).search([])
