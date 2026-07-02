"""End-to-end test: triage + agentic resolution + audit trail.

This test walks a single ticket through the entire user journey a support
operator would see in the UI, with the HTTP layer mocked. It proves the
following independently and together:

1. Triage transitions New -> AI Triaged and populates category, team,
   confidence, and cost.
2. Autonomy gating: with read_only autonomy, write tools are hidden and
   the loop falls back to escalation. With full autonomy on an approved
   category, write tools execute.
3. Agent loop executes multiple tool calls, respects the terminal
   escalate_to_human tool, and stops cleanly on end_turn.
4. Cost cap enforcement terminates the loop mid-flight with reason
   cost_cap.
5. Every tool invocation is persisted to ai.helpdesk.action with the
   correct sequence, tool name, JSON payloads, and success flag.
"""

import json
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged


class FakeResponse:
    """Small requests.Response stand-in for mocked provider calls."""

    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = "OK"

    def json(self):
        return self._payload


@tagged("post_install", "-at_install")
class TestAgentEndToEnd(TransactionCase):
    """Full lifecycle validation for the agentic helpdesk flow."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Icp = cls.env["ir.config_parameter"].sudo()
        Icp.set_param("ai_helpdesk_triage.provider", "anthropic")
        Icp.set_param("ai_helpdesk_triage.anthropic_api_key", "test-key")
        Icp.set_param("ai_helpdesk_triage.redact_pii", "False")
        Icp.set_param(
            "ai_helpdesk_triage.auto_route_confidence_threshold",
            "0.75",
        )
        Icp.set_param("ai_helpdesk_triage.review_confidence_threshold", "0.5")
        Icp.set_param("ai_helpdesk_triage.daily_budget_usd", "0")

        cls.partner = cls.env["res.partner"].create(
            {"name": "Jane Demo", "email": "jane.demo@example.com"},
        )
        cls.team = cls.env["ai.helpdesk.team"].create(
            {"name": "Billing Support", "description": "Billing and refunds"},
        )

    def setUp(self):
        super().setUp()
        Icp = self.env["ir.config_parameter"].sudo()
        Icp.set_param("ai_helpdesk_triage.autonomy_level", "full")
        Icp.set_param("ai_helpdesk_triage.autonomy_categories", "billing,technical")
        Icp.set_param("ai_helpdesk_triage.max_actions_per_ticket", "5")
        Icp.set_param("ai_helpdesk_triage.action_cost_cap_usd", "0.5")

    def _new_ticket(self, subject="Please resend my invoice", description=None):
        return self.env["ai.helpdesk.ticket"].create(
            {
                "name": subject,
                "description": description
                or (
                    "Hi, my finance team says they never received invoice "
                    "INV/2026/00042. Please resend the PDF to "
                    "jane.demo@example.com."
                ),
                "partner_id": self.partner.id,
                "partner_email": self.partner.email,
            },
        )

    def _triage_response(self, category="billing", confidence=0.92):
        return FakeResponse(
            {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "triage_ticket",
                        "input": {
                            "category": category,
                            "priority": "2",
                            "suggested_team": self.team.name,
                            "reasoning": (
                                "Customer reports missing invoice; the ask is " "clear and route is unambiguous."
                            ),
                            "suggested_reply": (
                                "We are resending the invoice now — please check " "your inbox in a few minutes."
                            ),
                            "confidence": confidence,
                            "sentiment": "neutral",
                            "urgency": "normal",
                            "is_ambiguous": False,
                        },
                    },
                ],
                "usage": {"input_tokens": 250, "output_tokens": 60},
            },
        )

    def _tool_use(self, tool_name, tool_input, use_id):
        return {
            "type": "tool_use",
            "id": use_id,
            "name": tool_name,
            "input": tool_input,
        }

    def _reflection_response(self, recommend="resolve", did_solve="yes", reason="Confirmed via tool result."):
        """Mock a Haiku reflection tool-use response."""
        return FakeResponse(
            {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "reflect_1",
                        "name": "reflect",
                        "input": {
                            "did_solve": did_solve,
                            "reason": reason,
                            "recommend": recommend,
                        },
                    },
                ],
                "usage": {"input_tokens": 50, "output_tokens": 20},
            },
        )

    def _loop_response(
        self,
        tool_uses,
        stop_reason="tool_use",
        assistant_text="",
        input_tokens=100,
        output_tokens=40,
    ):
        blocks = []
        if assistant_text:
            blocks.append({"type": "text", "text": assistant_text})
        blocks.extend(tool_uses)
        return FakeResponse(
            {
                "content": blocks,
                "stop_reason": stop_reason,
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                },
            },
        )

    # ------------------------------------------------------------------
    # 1. Triage happy path (proves the existing triage flow still works
    #    after the Bedrock provider dispatch refactor).
    # ------------------------------------------------------------------
    def test_1_triage_populates_and_transitions(self):
        """Triage moves New -> AI Triaged and captures cost + category."""
        ticket = self._new_ticket()
        self.assertEqual(ticket.state, "new")
        self.assertFalse(ticket.ai_triaged)

        with patch(
            "odoo.addons.ai_helpdesk_triage.models.helpdesk_ticket.requests.post",
            return_value=self._triage_response(),
        ) as mocked:
            ticket.action_ai_triage()

        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(ticket.state, "ai_triaged")
        self.assertTrue(ticket.ai_triaged)
        self.assertEqual(ticket.category, "billing")
        self.assertEqual(ticket.team_id, self.team)
        self.assertEqual(ticket.ai_review_status, "accepted")
        self.assertGreater(ticket.ai_total_cost, 0)
        self.assertEqual(ticket.ai_input_tokens, 250)
        self.assertEqual(ticket.ai_resolution_status, "not_attempted")

    # ------------------------------------------------------------------
    # 2. Agent loop resolves an invoice ticket end-to-end.
    #    lookup_customer -> post_customer_reply -> end_turn.
    # ------------------------------------------------------------------
    def test_2_agent_loop_resolves_with_multiple_tools(self):
        """Full agentic path: triage, then multi-turn tool loop, then resolved."""
        ticket = self._new_ticket()
        with patch(
            "odoo.addons.ai_helpdesk_triage.models.helpdesk_ticket.requests.post",
            return_value=self._triage_response(),
        ):
            ticket.action_ai_triage()

        # Scripted loop: two tool_use rounds, then a clean end_turn.
        loop_responses = [
            self._loop_response(
                [
                    self._tool_use(
                        "lookup_customer",
                        {"email": "jane.demo@example.com"},
                        "use_1",
                    ),
                ],
                assistant_text="Looking up the customer first.",
            ),
            self._loop_response(
                [
                    self._tool_use(
                        "post_customer_reply",
                        {"body": "Invoice resent. Please check your inbox."},
                        "use_2",
                    ),
                ],
                assistant_text="Confirming the resolution to the customer.",
            ),
            self._loop_response(
                [],
                stop_reason="end_turn",
                assistant_text=("Customer's invoice concern is addressed. Resolution complete."),
                input_tokens=80,
                output_tokens=30,
            ),
            # Reflection gate fires after a full-autonomy write; supply a
            # green-light response so the loop keeps the resolved verdict.
            self._reflection_response(),
        ]

        with patch(
            "odoo.addons.ai_helpdesk_triage.models.anthropic_client.requests.post",
            side_effect=loop_responses,
        ) as mocked:
            ticket.action_ai_resolve()

        self.assertEqual(
            mocked.call_count,
            4,
            "loop should call the API 3 times plus 1 reflection check",
        )
        self.assertEqual(ticket.ai_resolution_status, "resolved")
        self.assertFalse(ticket.ai_resolution_in_progress)
        self.assertEqual(ticket.ai_resolution_attempts, 1)
        self.assertGreater(ticket.ai_resolution_cost, 0)

        actions = ticket.action_ids.sorted("sequence")
        # 2 tool calls plus 1 reflection_check audit row.
        self.assertEqual(len(actions), 3)
        self.assertEqual(actions[0].tool_name, "lookup_customer")
        self.assertEqual(actions[1].tool_name, "post_customer_reply")
        self.assertEqual(actions[2].tool_name, "reflection_check")
        for action in actions[:2]:
            self.assertTrue(action.succeeded)
            payload = json.loads(action.tool_input)
            self.assertIsInstance(payload, dict)
            result = json.loads(action.tool_result)
            self.assertTrue(result.get("ok"))

        # Chatter carries a per-iteration note so a viewer can watch the
        # agent reason turn by turn instead of only seeing a final summary.
        iteration_notes = ticket.message_ids.filtered(
            lambda m: "AI iteration" in (m.body or ""),
        )
        # 3 loop iterations (lookup + reply + end_turn) each produced an
        # assistant text or tool call, so all 3 should be streamed.
        self.assertEqual(len(iteration_notes), 3)

    # ------------------------------------------------------------------
    # 2b. Reflection gate flips a shaky resolution to escalated.
    # ------------------------------------------------------------------
    def test_2b_reflection_rewrites_resolved_to_escalated(self):
        """When reflection says 'escalate', the loop returns escalated with reason=reflection_uncertain."""
        ticket = self._new_ticket()
        with patch(
            "odoo.addons.ai_helpdesk_triage.models.helpdesk_ticket.requests.post",
            return_value=self._triage_response(),
        ):
            ticket.action_ai_triage()

        loop_responses = [
            self._loop_response(
                [
                    self._tool_use(
                        "lookup_customer",
                        {"email": "jane.demo@example.com"},
                        "use_1",
                    ),
                ],
                assistant_text="Looking up the customer.",
            ),
            self._loop_response(
                [
                    self._tool_use(
                        "post_customer_reply",
                        {"body": "Invoice resent — check your inbox."},
                        "use_2",
                    ),
                ],
                assistant_text="Posting the confirmation.",
            ),
            self._loop_response(
                [],
                stop_reason="end_turn",
                assistant_text="Done.",
                input_tokens=80,
                output_tokens=30,
            ),
            # Reflection returns 'escalate' — resolution was shaky.
            self._reflection_response(
                recommend="escalate",
                did_solve="uncertain",
                reason="Could not confirm the invoice PDF actually reached the customer.",
            ),
        ]
        with patch(
            "odoo.addons.ai_helpdesk_triage.models.anthropic_client.requests.post",
            side_effect=loop_responses,
        ):
            ticket.action_ai_resolve()

        self.assertEqual(ticket.ai_resolution_status, "escalated")
        self.assertEqual(ticket.ai_resolution_reason, "reflection_uncertain")
        # The reflection call is recorded as an audit row so the timeline
        # shows why the ticket was escalated after apparent success.
        self.assertIn(
            "reflection_check",
            ticket.action_ids.mapped("tool_name"),
        )

    # ------------------------------------------------------------------
    # 3. Explicit escalation via escalate_to_human.
    # ------------------------------------------------------------------
    def test_3_escalate_to_human_terminates_with_reason(self):
        """Calling escalate_to_human ends the loop with status escalated."""
        ticket = self._new_ticket(
            subject="Data corrupted",
            description="Numbers on our dashboard have been wrong since Monday.",
        )
        with patch(
            "odoo.addons.ai_helpdesk_triage.models.helpdesk_ticket.requests.post",
            return_value=self._triage_response(category="technical"),
        ):
            ticket.action_ai_triage()

        loop_responses = [
            self._loop_response(
                [
                    self._tool_use(
                        "escalate_to_human",
                        {"reason": "Data investigation requires human access."},
                        "use_1",
                    ),
                ],
                assistant_text="Escalating to a human specialist.",
            ),
        ]
        with patch(
            "odoo.addons.ai_helpdesk_triage.models.anthropic_client.requests.post",
            side_effect=loop_responses,
        ):
            ticket.action_ai_resolve()

        self.assertEqual(ticket.ai_resolution_status, "escalated")
        self.assertEqual(len(ticket.action_ids), 1)
        self.assertEqual(ticket.action_ids.tool_name, "escalate_to_human")
        self.assertTrue(ticket.action_ids.succeeded)
        self.assertIn(
            "human",
            (ticket.ai_resolution_reason or "").lower(),
        )
        # Escalation autofills a to-do activity with the structured
        # what-I-tried narrative built from action_ids.
        escalation_activity = ticket.activity_ids.filtered(
            lambda a: "escalated" in (a.summary or "").lower(),
        )
        self.assertTrue(escalation_activity, "expected an escalation activity")
        note_text = escalation_activity[0].note or ""
        self.assertIn("escalate_to_human", note_text)
        self.assertIn("Escalating to a human specialist.", note_text)

    # ------------------------------------------------------------------
    # 4. Cost cap fires mid-loop.
    # ------------------------------------------------------------------
    def test_4_cost_cap_terminates_with_reason_cost_cap(self):
        """A tight cost cap forces escalation with reason cost_cap."""
        self.env["ir.config_parameter"].sudo().set_param(
            "ai_helpdesk_triage.action_cost_cap_usd",
            "0.0001",
        )
        ticket = self._new_ticket()
        with patch(
            "odoo.addons.ai_helpdesk_triage.models.helpdesk_ticket.requests.post",
            return_value=self._triage_response(),
        ):
            ticket.action_ai_triage()

        # Big token counts so estimated cost blows past the tiny cap on turn 1.
        response = self._loop_response(
            [
                self._tool_use(
                    "lookup_customer",
                    {"email": "jane.demo@example.com"},
                    "use_1",
                ),
            ],
            input_tokens=200_000,
            output_tokens=50_000,
        )
        with patch(
            "odoo.addons.ai_helpdesk_triage.models.anthropic_client.requests.post",
            return_value=response,
        ):
            ticket.action_ai_resolve()

        self.assertEqual(ticket.ai_resolution_status, "escalated")
        self.assertEqual(ticket.ai_resolution_reason, "cost_cap")

    # ------------------------------------------------------------------
    # 4b. Circuit breaker refuses resolve after too many recent failures.
    # ------------------------------------------------------------------
    def test_4b_circuit_breaker_refuses_after_failure_burst(self):
        """When >= threshold of recent actions in the category failed, refuse to resolve."""
        from odoo.exceptions import UserError as _UserError

        Icp = self.env["ir.config_parameter"].sudo()
        Icp.set_param("ai_helpdesk_triage.circuit_breaker_window_seconds", "3600")
        Icp.set_param("ai_helpdesk_triage.circuit_breaker_failure_threshold", "0.7")
        Icp.set_param("ai_helpdesk_triage.circuit_breaker_min_actions", "3")

        ticket = self._new_ticket()
        with patch(
            "odoo.addons.ai_helpdesk_triage.models.helpdesk_ticket.requests.post",
            return_value=self._triage_response(),
        ):
            ticket.action_ai_triage()

        # Seed 5 failed actions in the same category (billing) on a sibling ticket.
        sibling = self._new_ticket()
        sibling.category = "billing"
        Action = self.env["ai.helpdesk.action"].sudo()
        for i in range(5):
            Action.create(
                {
                    "ticket_id": sibling.id,
                    "sequence": i + 1,
                    "tool_name": "resend_invoice_pdf",
                    "tool_input": "{}",
                    "tool_result": '{"ok": false, "error": "send_failed"}',
                    "succeeded": False,
                    "error_message": "send_failed",
                },
            )

        with self.assertRaises(_UserError) as ctx:
            ticket.action_ai_resolve()
        self.assertIn("Circuit breaker", str(ctx.exception))

    # ------------------------------------------------------------------
    # 4c. Per-tool per-customer rate limit surfaces to the agent.
    # ------------------------------------------------------------------
    def test_4c_rate_limit_returns_error_without_calling_tool(self):
        """When the rate limit is breached, the tool callable does not run."""
        Icp = self.env["ir.config_parameter"].sudo()
        Icp.set_param(
            "ai_helpdesk_triage.tool_rate_limits",
            '{"send_password_reset": {"window_s": 3600, "max_calls": 1}}',
        )

        ticket = self._new_ticket()
        with patch(
            "odoo.addons.ai_helpdesk_triage.models.helpdesk_ticket.requests.post",
            return_value=self._triage_response(category="technical"),
        ):
            ticket.action_ai_triage()

        # Seed one successful reset for this customer so the second is over the cap.
        prior = self._new_ticket()
        prior.partner_id = self.partner
        self.env["ai.helpdesk.action"].sudo().create(
            {
                "ticket_id": prior.id,
                "sequence": 1,
                "tool_name": "send_password_reset",
                "tool_input": "{}",
                "tool_result": '{"ok": true}',
                "succeeded": True,
            },
        )

        loop_responses = [
            self._loop_response(
                [
                    self._tool_use(
                        "send_password_reset",
                        {"email": "jane.demo@example.com"},
                        "use_1",
                    ),
                ],
                assistant_text="Attempting the password reset.",
            ),
            # After a rate-limited result the agent has no next move — escalate.
            self._loop_response(
                [
                    self._tool_use(
                        "escalate_to_human",
                        {"reason": "rate limit hit."},
                        "use_2",
                    ),
                ],
                assistant_text="Escalating because rate limit hit.",
            ),
        ]
        with patch(
            "odoo.addons.ai_helpdesk_triage.models.anthropic_client.requests.post",
            side_effect=loop_responses,
        ):
            ticket.action_ai_resolve()

        # The first action must be a rate_limited row; the tool callable
        # never fired.
        rate_row = ticket.action_ids.filtered(lambda a: a.tool_name == "send_password_reset")
        self.assertTrue(rate_row)
        self.assertEqual(rate_row.error_message, "rate_limited")

    # ------------------------------------------------------------------
    # 4d. Circuit breaker ignores pre-execution refusal rows.
    # ------------------------------------------------------------------
    def test_4d_circuit_breaker_ignores_rate_limited_rows(self):
        """Rate-limited (and other policy-refusal) rows must not trip the breaker."""
        Icp = self.env["ir.config_parameter"].sudo()
        Icp.set_param("ai_helpdesk_triage.circuit_breaker_window_seconds", "3600")
        Icp.set_param("ai_helpdesk_triage.circuit_breaker_failure_threshold", "0.5")
        Icp.set_param("ai_helpdesk_triage.circuit_breaker_min_actions", "3")

        ticket = self._new_ticket()
        with patch(
            "odoo.addons.ai_helpdesk_triage.models.helpdesk_ticket.requests.post",
            return_value=self._triage_response(),
        ):
            ticket.action_ai_triage()

        # Seed 6 rows: 3 rate_limited (policy refusals) + 3 real success rows.
        # If pre-execution refusals were still counted, ratio = 3/6 = 0.5 and
        # the breaker would trip. With the filter, only the 3 successes are
        # counted → total < min_actions → breaker stays closed.
        sibling = self._new_ticket()
        sibling.category = "billing"
        Action = self.env["ai.helpdesk.action"].sudo()
        for i in range(3):
            Action.create(
                {
                    "ticket_id": sibling.id,
                    "sequence": i + 1,
                    "tool_name": "send_password_reset",
                    "tool_input": "{}",
                    "tool_result": '{"ok": false, "error": "rate_limited"}',
                    "succeeded": False,
                    "error_message": "rate_limited",
                },
            )
        for i in range(3):
            Action.create(
                {
                    "ticket_id": sibling.id,
                    "sequence": 10 + i,
                    "tool_name": "lookup_customer",
                    "tool_input": "{}",
                    "tool_result": '{"ok": true}',
                    "succeeded": True,
                },
            )

        # Should NOT raise — rate-limited rows are policy refusals, not
        # downstream failures, so the breaker's health signal ignores them.
        loop_responses = [
            self._loop_response(
                [
                    self._tool_use(
                        "escalate_to_human",
                        {"reason": "done"},
                        "u1",
                    ),
                ],
                assistant_text="escalating",
            ),
        ]
        with patch(
            "odoo.addons.ai_helpdesk_triage.models.anthropic_client.requests.post",
            side_effect=loop_responses,
        ):
            ticket.action_ai_resolve()
        # If the breaker had tripped, action_ai_resolve would have raised
        # a UserError. Reaching here proves it stayed closed.
        self.assertEqual(ticket.ai_resolution_status, "escalated")

    # ------------------------------------------------------------------
    # 5. Read-only autonomy hides write tools from the schema.
    # ------------------------------------------------------------------
    def test_5_read_only_autonomy_filters_write_tools(self):
        """With read_only autonomy, write tools are not offered to the model."""
        self.env["ir.config_parameter"].sudo().set_param(
            "ai_helpdesk_triage.autonomy_level",
            "read_only",
        )
        # Autonomy category gate doesn't apply for read_only, so no cat check.

        from odoo.addons.ai_helpdesk_triage.models import tool_registry

        available = tool_registry.get_available_tools(self.env, "read_only")
        self.assertIn("lookup_customer", available)
        self.assertIn("escalate_to_human", available)
        # Write tools MUST NOT be exposed
        self.assertNotIn("post_customer_reply", available)
        self.assertNotIn("send_password_reset", available)
        self.assertNotIn("resend_invoice_pdf", available)

        available_full = tool_registry.get_available_tools(self.env, "full")
        self.assertIn("post_customer_reply", available_full)

        available_off = tool_registry.get_available_tools(self.env, "off")
        self.assertEqual(available_off, {})

    # ------------------------------------------------------------------
    # 6. Full-autonomy category gate blocks non-approved categories.
    # ------------------------------------------------------------------
    def test_6_full_autonomy_category_gate(self):
        """Full autonomy on a non-approved category raises before HTTP."""
        self.env["ir.config_parameter"].sudo().set_param(
            "ai_helpdesk_triage.autonomy_categories",
            "billing",
        )
        ticket = self._new_ticket(
            subject="How to add a user?",
            description="General product question about seat management.",
        )
        with patch(
            "odoo.addons.ai_helpdesk_triage.models.helpdesk_ticket.requests.post",
            return_value=self._triage_response(category="general"),
        ):
            ticket.action_ai_triage()

        from odoo.exceptions import UserError

        with self.assertRaises(UserError):
            ticket.action_ai_resolve()

        self.assertEqual(ticket.ai_resolution_status, "not_attempted")
        self.assertEqual(len(ticket.action_ids), 0)
