# Live Demo Script — AI Helpdesk Triage

Three back-to-back scenarios you can run in ~2 minutes total. Each shows a
different behavior of the agent: direct autonomous fix, contextual
escalation, and a hard guardrail refusing autonomous action.

## What is already set up in the `myapp` DB

- **LLM provider:** Amazon Bedrock, model
  `us.anthropic.claude-sonnet-4-5-20250929-v1:0`, region `us-east-1`.
  Key stored in `ir.config_parameter` only.
- **Autonomy:** `full`. Approved categories: `billing,technical,general`.
- **Teams (all active, admin as member):**
  Technical Support, Billing Support, Customer Success.
- **Demo customer** (already exists as partner + user):
  Sarah Chen — `sarah.chen@demo.example.com`.
- **Tickets: none.** Cleared before each session.

If you need to reset between demos, on any ticket click **Reset to New**
in the header — that clears AI state and lets you re-run.

---

## Demo 1 — Autonomous password reset (fastest wow moment)

The AI takes direct action because the ask is simple and unambiguous.

**Fill in the New Ticket form**

| Field | Value |
|---|---|
| Subject | `Please push a fresh password reset link` |
| Customer | `Sarah Chen` (autocomplete picks her) |
| Category | leave blank |
| Priority | leave blank |
| Assigned Team | leave blank |
| Assigned To | leave blank |

**Description — paste this:**

```
Hi, I'd like to reset my password. The "forgot password" link
on the login page isn't sending me anything — could you push
a fresh reset email to sarah.chen@demo.example.com from your
end? Thanks.

- Sarah
```

**On stage**

1. Click **Save** — ticket lands, nothing fancy yet.
2. Click **AI Triage** — 3-5 seconds. Header status flips to `AI Triaged`.
   Category = Technical, Priority = Medium, Team = Technical Support.
   Confidence smart-button shows ~90 %.
3. Click **AI Resolve** — 5-10 seconds.
4. Open the **AI Actions** notebook tab. Expect this timeline:
   - `lookup_customer` — succeeded
   - `send_password_reset` — succeeded (Odoo actually queued the reset email)
   - `post_customer_reply` — succeeded (visible on chatter)
5. Kanban badge on the ticket: **AI Resolved** (green).

**Elapsed:** ~15 seconds total. **Cost:** ~$0.01.

**Talking point:** *"Simple, unambiguous ask → the AI just handles it. No human touched the ticket."*

---

## Demo 2 — Contextual escalation (the "AI knows when to hand off" story)

The AI enriches, schedules a follow-up, and explicitly escalates.

**Reset or create a new ticket**

| Field | Value |
|---|---|
| Subject | `Revenue numbers on our dashboard look wrong` |
| Customer | `Sarah Chen` |
| Category | leave blank |
| Priority | leave blank |

**Description — paste this:**

```
Since Monday our dashboard shows total revenue of $1.2M for
this week, but my finance team calculates it should be around
$850k. The discrepancy started Monday morning and I need to
present these numbers to our board on Friday. Please
investigate — this is a compliance concern.

Contact: sarah.chen@demo.example.com
```

**On stage**

1. **Save**, **AI Triage**. Category = Technical, Priority = Urgent,
   Team = Technical Support, confidence ~80 %.
2. **AI Resolve**. Expect these actions:
   - `lookup_customer` — enrichment
   - `list_customer_recent_activity` — pattern check
   - `create_team_activity` — schedules a to-do on Technical Support
     with a summary and note built from the ticket
   - `escalate_to_human` — terminal signal
3. Status becomes **Escalated** (amber badge).
4. Open the **Activities** panel on the chatter. Point at the to-do —
   assigned to a real team member with a deadline.

**Talking point:** *"When the stakes are high and the fix is not routine,
the AI does what a great support engineer would do — gather context,
brief the specialist, hand off. It doesn't try to be a hero."*

---

## Demo 3 — Guardrail firing (the "we control this" story)

The AI refuses to autonomously handle a ticket in a category the ops team
hasn't approved.

**Before this demo, tighten the settings temporarily**

1. **AI Helpdesk → Configuration → Settings**.
2. Under **Agentic Resolution**, edit **Categories approved for full
   autonomy** — change it to just `billing,technical` (remove
   `general`). Save.

**Create the ticket**

| Field | Value |
|---|---|
| Subject | `Can I add a second user to my subscription?` |
| Customer | `Sarah Chen` |

**Description — paste this:**

```
Hey, we're growing and I'd like to add my colleague as a
second user on our subscription so we can share the workload.
What's the process?

Thanks!
Sarah
```

**On stage**

1. **Save**, **AI Triage**. Expect Category = General, Team = Customer
   Success. Confidence high.
2. **AI Resolve**. Instead of running, you get a red error dialog:
   *"Category `general` is not approved for autonomous resolution."*
3. Ticket stays in `AI Triaged` — a human takes it from here.

**Talking point:** *"Adding a new tool to the AI does not automatically
expand its authority. A manager has to explicitly approve each
category. We can enable general later after we validate the model on a
sample."*

**Cleanup:** put `general` back in the approved list after the demo.

---

## The three-scenario story arc

| Demo | What the AI did | Human involvement | Talking point |
|---|---|---|---|
| 1 — Password reset | Took direct autonomous action | Zero | Boring tickets → fully solved |
| 2 — Revenue anomaly | Gathered context, scheduled a specialist to-do, escalated | Human picks up the well-briefed ticket | Judgment call, safer path |
| 3 — Seat invite | Refused to act autonomously | Human handles it manually | Ops keeps the leash |

Runs in about **two minutes**. Covers direct action, thoughtful handoff,
and hard governance — the three things every skeptical stakeholder wants
to see.

---

## If something goes wrong on stage

- **"Create at least one active AI Helpdesk team before triage"** — a
  team was deactivated. Go to **AI Helpdesk → Teams**, un-archive.
- **"Please select an assignee before assigning the ticket"** — you
  clicked **Assign**. Click **AI Resolve** instead.
- **"Agent resolution failed: module ... has no attribute ..."** — the
  running server has stale Python. Restart it and refresh the tab.
- **AI Resolve button not visible** — the ticket already has a
  resolution attempt. Click **Reset to New**.
- **Error mentioning Bedrock or 4xx** — check the API key setting under
  **Configuration → Settings → LLM Provider**. Verify the model ID
  matches something available in your Bedrock account.

## Reset for the next demo pass

To wipe every ticket and every action log so you can start clean:

```bash
python odoo-bin shell -c odoo.conf -d myapp --no-http --log-level=error
```

Then paste:

```python
env['ai.helpdesk.ticket'].sudo().search([]).unlink()
env.cr.commit()
```

Sarah, the teams, and the settings all stay in place. Only the tickets
and their action logs go away.
