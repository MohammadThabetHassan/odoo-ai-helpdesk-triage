"""Verify the per-tool kill switch actually removes tools from the schema."""

from odoo.addons.ai_helpdesk_triage.models.tool_registry import (
    get_available_tools,
    get_tool_schemas,
)
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestKillSwitch(TransactionCase):
    """The ai_helpdesk_triage.disabled_tools ir.config_parameter must filter
    tools out at every autonomy level and never let a disabled tool schema
    reach the model."""

    def setUp(self):
        super().setUp()
        # Ensure a clean slate — some other test may have left this set.
        self.env["ir.config_parameter"].sudo().set_param(
            "ai_helpdesk_triage.disabled_tools",
            "",
        )

    def test_empty_config_returns_full_toolset(self):
        """A blank kill list must not filter anything."""
        tools = get_available_tools(self.env, "full")
        names = set(tools.keys())
        self.assertIn("send_password_reset", names)
        self.assertIn("post_customer_reply", names)
        self.assertIn("lookup_customer", names)

    def test_disabled_write_tool_hidden_at_full(self):
        """Kill switch removes a write tool even at full autonomy."""
        self.env["ir.config_parameter"].sudo().set_param(
            "ai_helpdesk_triage.disabled_tools",
            "send_password_reset",
        )
        tools = get_available_tools(self.env, "full")
        self.assertNotIn("send_password_reset", tools)
        # Other write tools should still be present.
        self.assertIn("post_customer_reply", tools)

    def test_disabled_read_tool_hidden_at_read_only(self):
        """Kill switch removes a read tool even at read_only autonomy."""
        self.env["ir.config_parameter"].sudo().set_param(
            "ai_helpdesk_triage.disabled_tools",
            "lookup_customer",
        )
        tools = get_available_tools(self.env, "read_only")
        self.assertNotIn("lookup_customer", tools)

    def test_kill_list_supports_multiple_names(self):
        """Comma-separated names all disappear from the schema."""
        self.env["ir.config_parameter"].sudo().set_param(
            "ai_helpdesk_triage.disabled_tools",
            "send_password_reset, resend_invoice_pdf ,lookup_invoice",
        )
        tools = get_available_tools(self.env, "full")
        self.assertNotIn("send_password_reset", tools)
        self.assertNotIn("resend_invoice_pdf", tools)
        self.assertNotIn("lookup_invoice", tools)

    def test_get_tool_schemas_also_filters(self):
        """The schema list matches the same filter (nothing reaches the model)."""
        self.env["ir.config_parameter"].sudo().set_param(
            "ai_helpdesk_triage.disabled_tools",
            "escalate_to_human",
        )
        schemas = get_tool_schemas(self.env, "full")
        schema_names = {s["name"] for s in schemas}
        self.assertNotIn("escalate_to_human", schema_names)

    def test_requires_raw_pii_tools_hidden_when_redaction_on(self):
        """update_customer_contact must vanish from the schema when redact_pii is on."""
        Icp = self.env["ir.config_parameter"].sudo()
        Icp.set_param("ai_helpdesk_triage.redact_pii", "True")
        tools = get_available_tools(self.env, "full")
        self.assertNotIn("update_customer_contact", tools)

    def test_requires_raw_pii_tools_visible_when_redaction_off(self):
        """Turning redact_pii off surfaces the tool again."""
        Icp = self.env["ir.config_parameter"].sudo()
        Icp.set_param("ai_helpdesk_triage.redact_pii", "False")
        tools = get_available_tools(self.env, "full")
        self.assertIn("update_customer_contact", tools)
