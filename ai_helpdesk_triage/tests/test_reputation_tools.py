from odoo.tests.common import TransactionCase

from odoo.addons.ai_helpdesk_triage.models.tools.reputation_tools import (
    check_domain_reputation,
)


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