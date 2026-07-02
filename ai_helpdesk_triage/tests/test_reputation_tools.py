from odoo.addons.ai_helpdesk_triage.models.tools.reputation_tools import (
    _get_trusted_domains,
    check_domain_reputation,
)
from odoo.tests.common import TransactionCase


class TestDomainReputationTools(TransactionCase):
    def test_trusted_domain_static_allowlist(self):
        result = check_domain_reputation(self.env, self.env["ai.helpdesk.ticket"], "gmail.com")
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["verdict"], "trusted")

    def test_denylisted_domain(self):
        result = check_domain_reputation(self.env, self.env["ai.helpdesk.ticket"], "mailinator.com")
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["verdict"], "suspicious")

    def test_unknown_domain(self):
        result = check_domain_reputation(self.env, self.env["ai.helpdesk.ticket"], "randomunknownexample.com")
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["verdict"], "unknown")

    def test_invalid_domain_without_dot(self):
        result = check_domain_reputation(self.env, self.env["ai.helpdesk.ticket"], "localhost")
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["verdict"], "unknown")

    def test_partner_scan_uses_email_or_website_filter(self):
        """Trusted-domain lookup must not materialize partners without email or website."""
        blank = self.env["res.partner"].create({"name": "Blank Contact"})
        self.env["res.partner"].create(
            {"name": "Real Company", "email": "hello@realcompany.example"},
        )
        domains = _get_trusted_domains(self.env)
        self.assertIn("realcompany.example", domains)
        result = check_domain_reputation(
            self.env,
            self.env["ai.helpdesk.ticket"],
            "realcompany.example",
        )
        self.assertEqual(result["data"]["verdict"], "trusted")
        for domain in domains:
            self.assertNotIn(str(blank.id), domain)