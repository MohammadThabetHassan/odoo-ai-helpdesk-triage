# Packet A: Ticket Tags

Goal: add simple colored tags to tickets in about three commits. Do this on your own branch or machine.

## Commit 1: `feat(tags): add ai.helpdesk.tag model`

Edit `models/helpdesk_tag.py` and add:

```python
"""Ticket tags for AI Helpdesk."""

from odoo import fields, models


class HelpdeskTag(models.Model):
    """Small colored labels that users can attach to tickets."""

    _name = "ai.helpdesk.tag"
    _description = "AI Helpdesk Tag"
    _order = "name"

    name = fields.Char(required=True)
    color = fields.Integer()
```

Edit `models/__init__.py` and add:

```python
from . import helpdesk_tag
```

Edit `models/helpdesk_ticket.py` and add this field near `team_id`:

```python
tag_ids = fields.Many2many("ai.helpdesk.tag", string="Tags")
```

Run:

```bash
python -m py_compile server/odoo/Workshop/ai_helpdesk_triage/models/helpdesk_tag.py
```

## Commit 2: `feat(views): show ticket tags`

Edit `views/helpdesk_ticket_views.xml`.

In the form view, add this below `<field name="team_id"/>`:

```xml
<field name="tag_ids" widget="many2many_tags"/>
```

In the list view, add this after `<field name="team_id"/>`:

```xml
<field name="tag_ids" widget="many2many_tags" optional="show"/>
```

Run the XML parser check:

```bash
python -c "from xml.etree import ElementTree as ET; ET.parse('server/odoo/Workshop/ai_helpdesk_triage/views/helpdesk_ticket_views.xml')"
```

## Commit 3: `feat(security): add tag access and demo tags`

Edit `security/ir.model.access.csv` and add:

```csv
access_ai_helpdesk_tag_user,ai.helpdesk.tag.user,model_ai_helpdesk_tag,group_helpdesk_user,1,1,1,0
access_ai_helpdesk_tag_manager,ai.helpdesk.tag.manager,model_ai_helpdesk_tag,group_helpdesk_manager,1,1,1,1
```

Edit `data/demo_teams.xml` inside `<odoo noupdate="1">` and add:

```xml
<record id="tag_vip" model="ai.helpdesk.tag">
    <field name="name">VIP</field>
    <field name="color">2</field>
</record>
<record id="tag_escalated" model="ai.helpdesk.tag">
    <field name="name">Escalated</field>
    <field name="color">1</field>
</record>
```

## Local Test

```bash
python odoo-bin -d ai_helpdesk_test --stop-after-init -u ai_helpdesk_triage --test-enable --test-tags /ai_helpdesk_triage --addons-path=addons,server/odoo/Workshop
```

Then open a ticket and verify the Tags field appears on the form and list.

## Revert If Broken

```bash
git log --oneline -3
git revert <your_bad_commit_hash>
```
