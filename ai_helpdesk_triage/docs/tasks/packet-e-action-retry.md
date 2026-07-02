# Packet E — Manager retry for failed agent actions

**Suggested owner:** Yousuf Adeel

## Why this matters

When a tool call inside the resolution loop fails (e.g., the invoice template
was temporarily missing, or the mail queue was choked), the ticket often ends
in `escalated` status. A manager currently has to reset the ticket to New and
re-run the whole triage + resolution flow just to retry a single tool.

We need a targeted retry: from a failed `ai.helpdesk.action` row, a manager
should be able to re-execute just that tool with the same input and record
the outcome as a new sequence entry on the same ticket.

## Scope

1. Add a `Retry` button to the `ai.helpdesk.action` form view. Visible only
   when `succeeded == False`. Manager group only.
2. Implement the retry method on `ai.helpdesk.action`:
   - Parse `tool_input` JSON.
   - Look up the tool in `models.tool_registry.TOOL_REGISTRY`.
   - Execute inside a savepoint (mirror the pattern in `agent_loop._execute_tool`).
   - Create a new `ai.helpdesk.action` row with the next sequence number for
     the ticket, `ai_reasoning="Manager retry"`.
   - Update ticket-level totals via a small helper that recomputes
     `ai_resolution_cost` from all action rows (leave the cost field on the
     retry row itself at 0.0 — no new LLM call is made).
3. If the retry succeeds and the ticket was in `escalated` status, flip
   `ai_resolution_status` back to `resolved` and post a chatter message
   explaining that a manager retried the tool.

## Deliverables

- Modified `models/helpdesk_action.py`: `action_retry` method + a computed
  `is_retryable` field for the button visibility.
- Modified `views/helpdesk_action_views.xml`: add the button in the header.
- Modified `security/ir.model.access.csv` if needed (manager already has
  write access — check).
- New unit test in `tests/test_agent_actions.py` (create the file) verifying
  that a retry:
  - Creates a new row with an incremented sequence.
  - Uses the original `tool_input`.
  - Flips ticket status on success.

## Acceptance criteria

- Reset an existing failed action (or seed one) → click Retry → see a new
  action row appear on the ticket with the same tool and a fresh timestamp.
- Manager group can retry; user group cannot see the button.
- Cost cap and max_actions caps do NOT apply here — a retry is a manual,
  bounded action.

## Out of scope

- Bulk retry across multiple failed actions (single-record action for now).
- Editing the tool input before retrying (that's a separate ticket).
