# Packet D — Domain reputation lookup tool

**Suggested owner:** Rohith Sunil

## Why this matters

The agentic resolution loop currently only knows the customer's identity from
their partner record. When a ticket comes from an unknown email, the AI has no
signal about whether the domain is legitimate or throwaway. Adding a domain
reputation lookup gives the model one more input before it decides to send an
invoice PDF or a password reset.

## Scope

Add a new read-class tool `check_domain_reputation` to the agent registry.

For the first pass, keep the implementation deterministic: a curated allowlist
of trusted domains (`gmail.com`, `outlook.com`, and the customer companies'
domains) plus a denylist of known-throwaway providers. Anything else returns
`unknown`. A follow-up ticket can wire up a real reputation API later.

## Deliverables

1. New file `models/tools/reputation_tools.py` following the layout of
   `read_tools.py`:
   - Schema `CHECK_DOMAIN_REPUTATION_SCHEMA` describing one string param
     `domain`.
   - Function `check_domain_reputation(env, ticket, domain)` returning
     `{"ok": True, "data": {"domain": ..., "verdict": "trusted"|"suspicious"|"unknown"}}`.
   - A `REPUTATION_TOOLS` dict.
2. Register the tool in `models/tool_registry.py` by merging `REPUTATION_TOOLS`
   into `TOOL_REGISTRY`, class `read`.
3. Import the module in `models/tools/__init__.py`.
4. Extend the initial user message in `models/agent_loop.py:_initial_user_message`
   to mention that domain reputation is available (no code change needed if the
   tool description is clear — verify by reading the message assembly).
5. Add a targeted unit test in `tests/` following the `test_ai_parsing.py`
   style. Cover: trusted domain, denylisted domain, unknown domain, missing
   `.` in input.

## Acceptance criteria

- Upgrade completes cleanly: `python odoo-bin -u ai_helpdesk_triage -d <db> --stop-after-init`.
- With autonomy level `read_only`, the AI can call the new tool during a
  resolution attempt. Verify by creating a demo ticket with an unfamiliar
  email and checking the `AI Actions` timeline.
- New rows appear in the `AI Helpdesk → Configuration → AI Actions` list.

## Out of scope

- Real third-party reputation API integrations (leave hooks, no HTTP calls).
- Domain reputation caching to a new model — keep it stateless for now.
