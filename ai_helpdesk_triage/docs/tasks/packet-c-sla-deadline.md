# Packet C: SLA Deadline

Goal: add a simple deadline and overdue flag to tickets in about three commits.

## Commit 1: `feat(ticket): add deadline and overdue fields`

Edit `models/helpdesk_ticket.py`. The file already imports `fields`, so no new import is needed.


Add these fields near `user_id`:

```python
deadline = fields.Date(string="SLA Deadline")
is_overdue = fields.Boolean(compute="_compute_is_overdue", string="Overdue")
```

Add this method near the other compute methods:

```python
@api.depends("deadline", "state")
def _compute_is_overdue(self):
    """Flag open tickets whose SLA deadline has passed."""
    today = fields.Date.context_today(self)
    for ticket in self:
        ticket.is_overdue = bool(
            ticket.deadline
            and ticket.deadline < today
            and ticket.state not in ("resolved", "closed")
        )
```

Run:

```bash
python -m py_compile server/odoo/Workshop/ai_helpdesk_triage/models/helpdesk_ticket.py
```

## Commit 2: `feat(views): show deadline and overdue row decoration`

Edit `views/helpdesk_ticket_views.xml`.

In the form view, add this below `<field name="user_id"/>`:

```xml
<field name="deadline"/>
<field name="is_overdue" invisible="1"/>
```

In the list view, add `decoration-danger="priority == '3' or is_overdue"` by replacing the existing `decoration-danger="priority == '3'"`.

Also add this column after `user_id`:

```xml
<field name="deadline"/>
<field name="is_overdue" column_invisible="True"/>
```

## Commit 3: `feat(search): add overdue filter and kanban deadline`

In the search view, add this filter after Open:

```xml
<filter string="Overdue" name="overdue" domain="[('is_overdue','=',True)]"/>
```

In the kanban view, add the field declaration:

```xml
<field name="deadline"/>
<field name="is_overdue"/>
```

Inside the kanban card, add:

```xml
<div t-if="record.deadline.raw_value" class="mt-1">
    <i class="fa fa-clock-o" title="Deadline"/> <field name="deadline"/>
</div>
<span t-if="record.is_overdue.raw_value" class="badge text-bg-danger">Overdue</span>
```

## Local Test

Create a ticket with yesterday's deadline and verify the list row is red, the search filter finds it, and the kanban card shows the deadline.

```bash
python odoo-bin -d ai_helpdesk_test --stop-after-init -u ai_helpdesk_triage --test-enable --test-tags /ai_helpdesk_triage --addons-path=addons,server/odoo/Workshop
```

## Revert If Broken

```bash
git log --oneline -3
git revert <your_bad_commit_hash>
```

