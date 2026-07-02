# Packet F — Resolution SLA field and weekly report

**Suggested owner:** Ahmed Abd Ur Rehman

## Why this matters

Right now we track that a ticket was resolved by the AI, but we don't measure
how long each resolution took, and we don't have a way to show operations
whether the agent is getting faster or slower over time. Managers reviewing
autonomy expansions need this signal.

## Scope

Add two pieces:

1. A per-ticket resolution latency field.
2. A weekly pivot report grouped by category showing average latency and
   success rate.

## Deliverables

### 1. Ticket-level fields

In `models/helpdesk_ticket.py`, add:

- `ai_resolution_start_at` (Datetime, readonly) — stamped when
  `action_ai_resolve` first flips the ticket to `in_progress`.
- `ai_resolution_end_at` (Datetime, readonly) — stamped when
  `_apply_agent_loop_result` writes the terminal status.
- `ai_resolution_duration_minutes` (Float, computed, stored) — derived from
  the two above with `@api.depends`.

Wire the writes into the existing `action_ai_resolve` method and
`_apply_agent_loop_result` respectively — do not add new methods.

### 2. Report view

Create a new file `views/helpdesk_ticket_report_views.xml` with:

- A pivot view for `ai.helpdesk.ticket` grouped by `category` (row) and
  `ai_resolution_status` (col), measures = count and
  `ai_resolution_duration_minutes` average.
- A graph view: bar chart, x = category, y = average duration.
- An `ir.actions.act_window` named "AI Resolution Report".
- A new menu item under `menu_ai_helpdesk_config` sequence 40 called
  "Reports".

Register the file in `__manifest__.py` under `data`.

### 3. Weekly digest cron (optional stretch)

If you have time, add an inactive-by-default `ir.cron` that once a week
posts a summary of the last 7 days' resolution counts and average duration
to a manager's activity queue. Keep it inactive on install — managers
enable it if they want it.

## Acceptance criteria

- Existing tickets show `0.0` duration until a fresh resolve run stamps
  both timestamps. Verified by creating one demo ticket end-to-end.
- The Reports menu appears only for managers.
- The pivot renders with real numbers after 2-3 demo resolves.

## Out of scope

- Emailed HTML report (activity queue only if you build the stretch).
- Per-tool timing breakdown (that lives on `ai.helpdesk.action` already).
- Historical backfill of `ai_resolution_start_at` for old tickets — leave
  them at False.
