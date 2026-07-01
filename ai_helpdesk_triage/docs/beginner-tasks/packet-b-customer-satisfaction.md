# Packet B: Customer Satisfaction

Goal: add a tiny customer satisfaction marker to resolved tickets in about three commits.

## Commit 1: `feat(ticket): add satisfaction field`

Edit `models/helpdesk_ticket.py` and add this field near `priority`:

```python
satisfaction = fields.Selection(
    [
        ("happy", "Happy"),
        ("neutral", "Neutral"),
        ("unhappy", "Unhappy"),
    ],
    string="Customer Satisfaction",
)
```

Run:

```bash
python -m py_compile server/odoo/Workshop/ai_helpdesk_triage/models/helpdesk_ticket.py
```

## Commit 2: `feat(views): show satisfaction on resolved tickets`

Edit `views/helpdesk_ticket_views.xml`.

In the form view, add this below `<field name="priority" widget="priority"/>`:

```xml
<field name="satisfaction" readonly="state not in ('resolved', 'closed')"/>
```

This means the field is visible all the time, but editable only after the ticket is resolved or closed.

## Commit 3: `feat(search): add satisfaction filter and group-by`

In the ticket search view, add this after the Urgent filter:

```xml
<filter string="Unhappy" name="unhappy" domain="[('satisfaction','=','unhappy')]"/>
```

Inside the `<group>` block, add:

```xml
<filter string="Satisfaction" name="group_satisfaction" context="{'group_by': 'satisfaction'}"/>
```

Run XML parser check:

```bash
python -c "from xml.etree import ElementTree as ET; ET.parse('server/odoo/Workshop/ai_helpdesk_triage/views/helpdesk_ticket_views.xml')"
```

## Local Test

Update the module, open a ticket, move it to Resolved, and verify Satisfaction becomes editable.

```bash
python odoo-bin -d ai_helpdesk_test --stop-after-init -u ai_helpdesk_triage --test-enable --test-tags /ai_helpdesk_triage --addons-path=addons,server/odoo/Workshop
```

## Revert If Broken

```bash
git log --oneline -3
git revert <your_bad_commit_hash>
```
