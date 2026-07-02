"""Unit tests for the pure decision helpers inside agent_loop.

These do not exercise the loop end-to-end — that's what the e2e suite is
for. Here we lock in the routing / reflection heuristics so future
prompt tuning does not silently change which model gets called.
"""

from odoo.addons.ai_helpdesk_triage.models import agent_loop, anthropic_client
from odoo.tests.common import TransactionCase, tagged

FULL_SCHEMAS = [
    {"name": "lookup_customer"},
    {"name": "send_password_reset"},
    {"name": "escalate_to_human"},
]
READ_ONLY_SCHEMAS = [
    {"name": "lookup_customer"},
    {"name": "escalate_to_human"},
]


@tagged("post_install", "-at_install")
class TestChooseIterationModel(TransactionCase):
    """_choose_iteration_model picks Sonnet or Haiku based on loop state."""

    def _choose(self, **overrides):
        args = {
            "iteration": 1,
            "action_tool_classes": [],
            "is_ambiguous": False,
            "urgency": "normal",
            "sticky_sonnet": False,
            "full_schemas": FULL_SCHEMAS,
            "read_only_schemas": READ_ONLY_SCHEMAS,
        }
        args.update(overrides)
        return agent_loop._choose_iteration_model(**args)

    def test_first_iteration_is_always_sonnet(self):
        """Iteration 1 plans on Sonnet with the full toolset."""
        model, schemas = self._choose(iteration=1)
        self.assertEqual(model, anthropic_client.DEFAULT_MODEL)
        self.assertEqual(schemas, FULL_SCHEMAS)

    def test_iteration_2_after_one_read_stays_on_sonnet(self):
        """The happy path (read once, then write) needs Sonnet on turn 2."""
        model, schemas = self._choose(iteration=2, action_tool_classes=["read"])
        self.assertEqual(model, anthropic_client.DEFAULT_MODEL)
        self.assertEqual(schemas, FULL_SCHEMAS)

    def test_haiku_after_two_reads(self):
        """Two reads without a write mean an investigation — hand it to Haiku."""
        model, schemas = self._choose(
            iteration=3,
            action_tool_classes=["read", "read"],
        )
        self.assertEqual(model, anthropic_client.FAST_MODEL)
        self.assertEqual(schemas, READ_ONLY_SCHEMAS)

    def test_write_action_forces_sonnet_next(self):
        """After any write, the next iteration falls back to Sonnet with all tools."""
        model, schemas = self._choose(
            iteration=3,
            action_tool_classes=["read", "write"],
        )
        self.assertEqual(model, anthropic_client.DEFAULT_MODEL)
        self.assertEqual(schemas, FULL_SCHEMAS)

    def test_ambiguous_always_uses_sonnet(self):
        """An ambiguous ticket forces Sonnet even after multiple reads."""
        model, schemas = self._choose(
            iteration=3,
            action_tool_classes=["read", "read"],
            is_ambiguous=True,
        )
        self.assertEqual(model, anthropic_client.DEFAULT_MODEL)
        self.assertEqual(schemas, FULL_SCHEMAS)

    def test_vip_urgency_always_uses_sonnet(self):
        """VIP customers always get the most capable model."""
        model, _schemas = self._choose(
            iteration=3,
            action_tool_classes=["read", "read"],
            urgency="vip",
        )
        self.assertEqual(model, anthropic_client.DEFAULT_MODEL)

    def test_sticky_sonnet_pins_the_model(self):
        """Once a Haiku turn produced bad_arguments the loop pins to Sonnet."""
        model, _schemas = self._choose(
            iteration=4,
            action_tool_classes=["read", "read", "read"],
            sticky_sonnet=True,
        )
        self.assertEqual(model, anthropic_client.DEFAULT_MODEL)


@tagged("post_install", "-at_install")
class TestShouldReflect(TransactionCase):
    """_should_reflect gates the pre-return self-critique correctly."""

    def _should(self, **overrides):
        args = {
            "action_tool_classes": [],
            "autonomy_level": "full",
            "urgency": "normal",
            "actions_count": 0,
            "total_cost": 0.0,
            "cost_cap": 0.5,
        }
        args.update(overrides)
        return agent_loop._should_reflect(**args)

    def test_no_reflection_for_trivial_read_only_run(self):
        """A single read action under read_only autonomy skips reflection."""
        self.assertFalse(
            self._should(
                action_tool_classes=["read"],
                autonomy_level="read_only",
                actions_count=1,
                total_cost=0.001,
            ),
        )

    def test_vip_urgency_forces_reflection(self):
        """VIP tickets always get double-checked before resolving."""
        self.assertTrue(self._should(urgency="vip", actions_count=1))

    def test_three_or_more_actions_forces_reflection(self):
        """Long paths always get double-checked."""
        self.assertTrue(
            self._should(
                actions_count=3,
                action_tool_classes=["read", "read", "read"],
            ),
        )

    def test_spending_over_half_cap_forces_reflection(self):
        """Cost pressure is a signal of uncertainty — reflect."""
        self.assertTrue(self._should(total_cost=0.30, cost_cap=0.5))

    def test_last_action_write_at_full_autonomy_forces_reflection(self):
        """A real-world write side effect under full autonomy always reflects."""
        self.assertTrue(
            self._should(
                action_tool_classes=["read", "write"],
                autonomy_level="full",
                actions_count=2,
            ),
        )

    def test_last_action_write_at_read_only_does_not_reflect(self):
        """Read-only autonomy can't have executed a write; skip reflection."""
        # This is a defensive check — write classes shouldn't reach here at
        # read_only autonomy, but if they did the gate is autonomy-scoped.
        self.assertFalse(
            self._should(
                action_tool_classes=["write"],
                autonomy_level="read_only",
                actions_count=1,
            ),
        )
