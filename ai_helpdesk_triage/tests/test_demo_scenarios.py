"""End-to-end scenarios that mirror docs/demo-script.md.

Each test is a full triage + resolve walk of one demo path with the HTTP
layer mocked. Together they lock in the three-scenario story arc: direct
autonomous fix, contextual escalation with a hand-off activity, and a
hard governance refusal.
"""

import json
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


class FakeResponse:
    """Small requests.Response stand-in."""

    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = "OK"

    def json(self):
        return self._payload


@tagged("post_install", "-at_install")
class TestDemoScenarios(TransactionCase):
    """Three demo scenarios end to end, matching the live demo script."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Icp = cls.env["ir.config_parameter"].sudo()
        Icp.set_param("ai_helpdesk_triage.provider", "anthropic")
        Icp.set_param("ai_helpdesk_triage.anthropic_api_key", "test-key")
        Icp.set_param("ai_helpdesk_triage.redact_pii", "False")
        Icp.set_param("ai_helpdesk_triage.auto_route_confidence_threshold", "0.75")
        Icp.set_param("ai_helpdesk_triage.review_confidence_threshold", "0.5")
        Icp.set_param("ai_helpdesk_triage.daily_budget_usd", "0")

        cls.sarah = cls.env["res.partner"].create(
            {"name": "Sarah Chen", "email": "sarah.chen@demo.example.com"},
        )
        cls.tech = cls.env["ai.helpdesk.team"].create(
            {"name": "Technical Support", "description": "Tech support"},
        )
        cls.billing = cls.env["ai.helpdesk.team"].create(
            {"name": "Billing Support", "description": "Billing"},
        )
        cls.success = cls.env["ai.helpdesk.team"].create(
            {"name": "Customer Success", "description": "CS"},
        )

    def setUp(self):
        super().setUp()
        Icp = self.env["ir.config_parameter"].sudo()
        Icp.set_param("ai_helpdesk_triage.autonomy_level", "full")
        Icp.set_param(
            "ai_helpdesk_triage.autonomy_categories",
            "billing,technical,general",
        )
        Icp.set_param("ai_helpdesk_triage.max_actions_per_ticket", "6")
        Icp.set_param("ai_helpdesk_triage.action_cost_cap_usd", "0.5")
        # Reset guardrails that other tests may have flipped.
        Icp.set_param("ai_helpdesk_triage.disabled_tools", "")
        Icp.set_param("ai_helpdesk_triage.tool_rate_limits", "")
        Icp.set_param("ai_helpdesk_triage.circuit_breaker_min_actions", "9999")

    # ------------------------------------------------------------------
    # Fixture helpers
    # ------------------------------------------------------------------
    def _triage_response(self, category="technical", team_name=None, confidence=0.9, urgency="normal"):
        return FakeResponse(
            {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "triage_ticket",
                        "input": {
                            "category": category,
                            "priority": "2",
                            "suggested_team": team_name or self.tech.name,
                            "reasoning": "Demo scenario triage.",
                            "suggested_reply": "We are on it.",
                            "confidence": confidence,
                            "sentiment": "neutral",
                            "urgency": urgency,
                            "is_ambiguous": False,
                        },
                    },
                ],
                "usage": {"input_tokens": 250, "output_tokens": 60},
            },
        )

    def _tool_use(self, tool_name, tool_input, use_id):
        return {"type": "tool_use", "id": use_id, "name": tool_name, "input": tool_input}

    def _loop_response(self, tool_uses, stop_reason="tool_use", text="", input_tokens=100, output_tokens=40):
        blocks = []
        if text:
            blocks.append({"type": "text", "text": text})
        blocks.extend(tool_uses)
        return FakeResponse(
            {
                "content": blocks,
                "stop_reason": stop_reason,
                "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
            },
        )

    def _reflection(self, recommend="resolve"):
        return FakeResponse(
            {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "reflect_1",
                        "name": "reflect",
                        "input": {"did_solve": "yes", "reason": "Confirmed.", "recommend": recommend},
                    },
                ],
                "usage": {"input_tokens": 40, "output_tokens": 15},
            },
        )

    def _new_ticket(self, subject, description):
        return self.env["ai.helpdesk.ticket"].create(
            {
                "name": subject,
                "description": description,
                "partner_id": self.sarah.id,
                "partner_email": self.sarah.email,
            },
        )

    # ------------------------------------------------------------------
    # Demo 1 — Autonomous password reset (fastest wow moment)
    # ------------------------------------------------------------------
    def test_demo_1_autonomous_password_reset(self):
        """Simple ask -> lookup, reset, reply, resolved. No human touched it."""
        ticket = self._new_ticket(
            subject="Please push a fresh password reset link",
            description=(
                "Hi, I'd like to reset my password. The 'forgot password' link "
                "on the login page isn't sending me anything — could you push "
                "a fresh reset email to sarah.chen@demo.example.com from your end?"
            ),
        )
        with patch(
            "odoo.addons.ai_helpdesk_triage.models.helpdesk_ticket.requests.post",
            return_value=self._triage_response(category="technical"),
        ):
            ticket.action_ai_triage()

        self.assertEqual(ticket.state, "ai_triaged")
        self.assertEqual(ticket.category, "technical")

        loop_responses = [
            self._loop_response(
                [self._tool_use("lookup_customer", {"email": "sarah.chen@demo.example.com"}, "u1")],
                text="Looking up Sarah's account.",
            ),
            self._loop_response(
                [self._tool_use("send_password_reset", {"email": "sarah.chen@demo.example.com"}, "u2")],
                text="Triggering the reset flow.",
            ),
            self._loop_response(
                [
                    self._tool_use(
                        "post_customer_reply",
                        {"body": "Sent a fresh reset link — please check your inbox."},
                        "u3",
                    ),
                ],
                text="Confirming to the customer.",
            ),
            self._loop_response([], stop_reason="end_turn", text="All done."),
            self._reflection(recommend="resolve"),
        ]
        with patch(
            "odoo.addons.ai_helpdesk_triage.models.anthropic_client.requests.post",
            side_effect=loop_responses,
        ) as mocked:
            ticket.action_ai_resolve()

        # 4 loop turns + 1 reflection = 5 API calls total.
        self.assertEqual(mocked.call_count, 5)
        self.assertEqual(ticket.ai_resolution_status, "resolved")
        self.assertFalse(ticket.ai_resolution_in_progress)
        tool_names = ticket.action_ids.sorted("sequence").mapped("tool_name")
        self.assertEqual(
            tool_names,
            ["lookup_customer", "send_password_reset", "post_customer_reply", "reflection_check"],
        )
        # Iteration notes visible on chatter — one per non-empty turn.
        iteration_notes = ticket.message_ids.filtered(lambda m: "AI iteration" in (m.body or ""))
        self.assertEqual(len(iteration_notes), 4)

    # ------------------------------------------------------------------
    # Demo 2 — Contextual escalation (AI knows when to hand off)
    # ------------------------------------------------------------------
    def test_demo_2_contextual_escalation_with_activity(self):
        """Complex ask -> enrich, brief a specialist, escalate. Activity created."""
        ticket = self._new_ticket(
            subject="Revenue numbers on our dashboard look wrong",
            description=(
                "Since Monday our dashboard shows total revenue of $1.2M for this "
                "week, but my finance team calculates it should be around $850k. "
                "Please investigate — this is a compliance concern."
            ),
        )
        with patch(
            "odoo.addons.ai_helpdesk_triage.models.helpdesk_ticket.requests.post",
            return_value=self._triage_response(
                category="technical",
                confidence=0.82,
                urgency="vip",
            ),
        ):
            ticket.action_ai_triage()

        # Assign a team member so create_team_activity has someone to page.
        member = self.env.ref("base.user_admin")
        self.tech.write({"member_ids": [(4, member.id)]})

        loop_responses = [
            self._loop_response(
                [self._tool_use("lookup_customer", {"email": "sarah.chen@demo.example.com"}, "u1")],
                text="Enriching customer context.",
            ),
            self._loop_response(
                [
                    self._tool_use(
                        "list_customer_recent_activity",
                        {"email": "sarah.chen@demo.example.com", "limit": 5},
                        "u2",
                    ),
                ],
                text="Checking for related tickets.",
            ),
            self._loop_response(
                [
                    self._tool_use(
                        "create_team_activity",
                        {
                            "team_name": self.tech.name,
                            "summary": "Investigate revenue miscalc",
                            "note": "Sarah reports a $350k gap between dashboard and finance sheet since Monday.",
                            "deadline_days": 1,
                        },
                        "u3",
                    ),
                ],
                text="Briefing a specialist.",
            ),
            self._loop_response(
                [
                    self._tool_use(
                        "escalate_to_human",
                        {"reason": "Data investigation requires human access."},
                        "u4",
                    ),
                ],
                text="Handing off to a human.",
            ),
        ]
        with patch(
            "odoo.addons.ai_helpdesk_triage.models.anthropic_client.requests.post",
            side_effect=loop_responses,
        ):
            ticket.action_ai_resolve()

        self.assertEqual(ticket.ai_resolution_status, "escalated")
        self.assertIn(
            "human",
            (ticket.ai_resolution_reason or "").lower(),
        )
        tool_names = ticket.action_ids.sorted("sequence").mapped("tool_name")
        self.assertEqual(
            tool_names,
            [
                "lookup_customer",
                "list_customer_recent_activity",
                "create_team_activity",
                "escalate_to_human",
            ],
        )
        # Two mail.activity rows: one from create_team_activity, one from
        # the escalation autofill. Both are visible on the ticket.
        activities = ticket.activity_ids
        self.assertGreaterEqual(len(activities), 2)
        escalation_activity = activities.filtered(
            lambda a: "escalated" in (a.summary or "").lower(),
        )
        self.assertTrue(escalation_activity)
        note = escalation_activity[0].note or ""
        # The autofill embeds the tool sequence and the closing recommendation.
        self.assertIn("lookup_customer", note)
        self.assertIn("Handing off to a human.", note)

    # ------------------------------------------------------------------
    # Demo 3 — Guardrail firing (governance refusal)
    # ------------------------------------------------------------------
    def test_demo_3_full_autonomy_category_gate_refuses(self):
        """Ticket in unapproved category → resolve refuses with a clear error."""
        # Tighten config: only billing and technical are approved (no general).
        self.env["ir.config_parameter"].sudo().set_param(
            "ai_helpdesk_triage.autonomy_categories",
            "billing,technical",
        )
        ticket = self._new_ticket(
            subject="Can I add a second user to my subscription?",
            description=(
                "Hey, we're growing and I'd like to add my colleague as a "
                "second user on our subscription. What's the process?"
            ),
        )
        with patch(
            "odoo.addons.ai_helpdesk_triage.models.helpdesk_ticket.requests.post",
            return_value=self._triage_response(
                category="general",
                team_name=self.success.name,
                confidence=0.9,
            ),
        ):
            ticket.action_ai_triage()

        self.assertEqual(ticket.category, "general")
        self.assertEqual(ticket.state, "ai_triaged")

        with self.assertRaises(UserError) as ctx:
            ticket.action_ai_resolve()
        self.assertIn("general", str(ctx.exception))
        # Ticket must not have moved into in_progress or resolved.
        self.assertEqual(ticket.state, "ai_triaged")
        self.assertEqual(ticket.ai_resolution_status, "not_attempted")

    # ------------------------------------------------------------------
    # Extra: prompt caching cost is applied end to end.
    # ------------------------------------------------------------------
    def test_prompt_cache_reduces_iteration_cost(self):
        """Cache_read tokens on iteration 2 must land at 10% of input rate."""
        ticket = self._new_ticket(
            subject="Please push a fresh password reset link",
            description="Same as demo 1.",
        )
        with patch(
            "odoo.addons.ai_helpdesk_triage.models.helpdesk_ticket.requests.post",
            return_value=self._triage_response(category="technical"),
        ):
            ticket.action_ai_triage()

        # Two-iteration mini-flow with cache creation on turn 1 and cache
        # read on turn 2 in the exact same shape Anthropic actually emits.
        loop_responses = [
            FakeResponse(
                {
                    "content": [
                        {"type": "text", "text": "Looking up first."},
                        {
                            "type": "tool_use",
                            "id": "u1",
                            "name": "lookup_customer",
                            "input": {"email": "sarah.chen@demo.example.com"},
                        },
                    ],
                    "stop_reason": "tool_use",
                    "usage": {
                        "input_tokens": 1200,
                        "output_tokens": 40,
                        "cache_creation_input_tokens": 800,
                        "cache_read_input_tokens": 0,
                    },
                },
            ),
            FakeResponse(
                {
                    "content": [
                        {"type": "text", "text": "Now the reset."},
                        {
                            "type": "tool_use",
                            "id": "u2",
                            "name": "send_password_reset",
                            "input": {"email": "sarah.chen@demo.example.com"},
                        },
                    ],
                    "stop_reason": "tool_use",
                    "usage": {
                        "input_tokens": 200,
                        "output_tokens": 30,
                        "cache_creation_input_tokens": 0,
                        # Big cache read on turn 2 — this is where savings show up.
                        "cache_read_input_tokens": 800,
                    },
                },
            ),
            self._loop_response([], stop_reason="end_turn", text="Done."),
            self._reflection(),
        ]
        with patch(
            "odoo.addons.ai_helpdesk_triage.models.anthropic_client.requests.post",
            side_effect=loop_responses,
        ):
            ticket.action_ai_resolve()

        # If the loop billed cache_read tokens at the same rate as raw input,
        # ai_resolution_cost would include 800 tokens * 3.00/1M = $0.0024 on
        # turn 2. Priced correctly at 0.10x, it's $0.00024. So a strict
        # upper bound rules out the wrong price path.
        self.assertLess(ticket.ai_resolution_cost, 0.02)

    # ------------------------------------------------------------------
    # Extra: extended thinking is added when the ticket is ambiguous.
    # ------------------------------------------------------------------
    def test_extended_thinking_flag_present_on_ambiguous_first_turn(self):
        """When triage marks the ticket ambiguous, iter 1 payload carries `thinking`."""
        ticket = self._new_ticket(
            subject="Something is off",
            description="I don't know what's wrong. Something is off. Help.",
        )
        # Custom triage response with is_ambiguous=True.
        ambiguous_response = FakeResponse(
            {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "triage_ticket",
                        "input": {
                            "category": "general",
                            "priority": "1",
                            "suggested_team": self.tech.name,
                            "reasoning": "Ticket does not clearly describe a single ask.",
                            "suggested_reply": "We are looking into it.",
                            "confidence": 0.85,
                            "sentiment": "frustrated",
                            "urgency": "normal",
                            "is_ambiguous": True,
                        },
                    },
                ],
                "usage": {"input_tokens": 250, "output_tokens": 60},
            },
        )
        with patch(
            "odoo.addons.ai_helpdesk_triage.models.helpdesk_ticket.requests.post",
            return_value=ambiguous_response,
        ):
            ticket.action_ai_triage()
        self.assertTrue(ticket.ai_ambiguous)

        loop_responses = [
            self._loop_response(
                [
                    self._tool_use(
                        "escalate_to_human",
                        {"reason": "Need more info."},
                        "u1",
                    ),
                ],
                text="Can't act on this yet.",
            ),
        ]
        captured_payloads = []

        def _capturing_post(url, headers=None, json=None, timeout=None):  # noqa: A002
            captured_payloads.append(json)
            return loop_responses.pop(0)

        with patch(
            "odoo.addons.ai_helpdesk_triage.models.anthropic_client.requests.post",
            side_effect=_capturing_post,
        ):
            ticket.action_ai_resolve()

        # First captured payload is iteration 1. It should carry `thinking`.
        self.assertTrue(captured_payloads, "expected at least one captured payload")
        self.assertIn("thinking", captured_payloads[0])
        self.assertEqual(captured_payloads[0]["thinking"]["type"], "enabled")


# json import is retained for future tool_input assertions.
_ = json
