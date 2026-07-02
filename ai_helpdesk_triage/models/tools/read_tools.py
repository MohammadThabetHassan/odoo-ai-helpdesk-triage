"""Read-only lookup tools available to the agent under any autonomy level."""

from __future__ import annotations


LOOKUP_CUSTOMER_SCHEMA = {
    "name": "lookup_customer",
    "description": (
        "Look up a customer by email address. Returns their name, phone, "
        "open helpdesk ticket count, and whether they exist as a partner."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "email": {
                "type": "string",
                "description": "The customer's email address.",
                "minLength": 3,
            },
        },
        "required": ["email"],
    },
}

LOOKUP_INVOICE_SCHEMA = {
    "name": "lookup_invoice",
    "description": (
        "Look up a customer invoice by its human reference (e.g. INV/2026/00042). "
        "Returns status, amount, currency, due date. Only works if the accounting "
        "module is installed."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "reference": {
                "type": "string",
                "description": "Invoice reference or number.",
                "minLength": 3,
            },
        },
        "required": ["reference"],
    },
}

LIST_RECENT_ACTIVITY_SCHEMA = {
    "name": "list_customer_recent_activity",
    "description": (
        "List the most recent helpdesk tickets for a customer (by email). "
        "Useful to detect repeat issues before deciding on an action."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "email": {"type": "string", "minLength": 3},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
        },
        "required": ["email"],
    },
}


def lookup_customer(env, ticket, email):
    """Find a customer partner by email, returning a compact profile."""
    partner = env["res.partner"].sudo().search(
        [("email", "=ilike", email)],
        limit=1,
    )
    if not partner:
        return {
            "ok": True,
            "data": {"found": False, "email": email},
        }
    open_tickets = env["ai.helpdesk.ticket"].sudo().search_count(
        [
            ("partner_id", "=", partner.id),
            ("state", "not in", ("resolved", "closed")),
        ],
    )
    return {
        "ok": True,
        "data": {
            "found": True,
            "partner_id": partner.id,
            "name": partner.display_name,
            "email": partner.email or "",
            "phone": partner.phone or "",
            "open_ticket_count": open_tickets,
        },
    }


def lookup_invoice(env, ticket, reference):
    """Return invoice status/amount by reference, or an error if module absent."""
    if not _module_installed(env, "account"):
        return {"ok": False, "error": "module_not_installed", "module": "account"}
    invoice = env["account.move"].sudo().search(
        [("name", "=", reference), ("move_type", "in", ("out_invoice", "out_refund"))],
        limit=1,
    )
    if not invoice:
        return {"ok": True, "data": {"found": False, "reference": reference}}
    return {
        "ok": True,
        "data": {
            "found": True,
            "reference": invoice.name,
            "state": invoice.state,
            "payment_state": getattr(invoice, "payment_state", "unknown"),
            "amount_total": invoice.amount_total,
            "currency": invoice.currency_id.name or "",
            "invoice_date": str(invoice.invoice_date or ""),
            "invoice_date_due": str(invoice.invoice_date_due or ""),
            "partner_name": invoice.partner_id.display_name,
        },
    }


def list_customer_recent_activity(env, ticket, email, limit=5):
    """List recent helpdesk tickets for a customer by email."""
    partner = env["res.partner"].sudo().search(
        [("email", "=ilike", email)],
        limit=1,
    )
    if not partner:
        return {"ok": True, "data": {"tickets": []}}
    tickets = env["ai.helpdesk.ticket"].sudo().search(
        [("partner_id", "=", partner.id)],
        limit=max(1, min(int(limit or 5), 20)),
        order="create_date desc",
    )
    return {
        "ok": True,
        "data": {
            "tickets": [
                {
                    "id": t.id,
                    "subject": t.name,
                    "state": t.state,
                    "category": t.category or "",
                    "created": str(t.create_date),
                }
                for t in tickets
            ],
        },
    }


def _module_installed(env, name):
    """Check whether an Odoo module is installed in the current DB."""
    return bool(
        env["ir.module.module"]
        .sudo()
        .search_count([("name", "=", name), ("state", "=", "installed")]),
    )


READ_TOOLS = {
    "lookup_customer": {
        "schema": LOOKUP_CUSTOMER_SCHEMA,
        "callable": lookup_customer,
        "class": "read",
    },
    "lookup_invoice": {
        "schema": LOOKUP_INVOICE_SCHEMA,
        "callable": lookup_invoice,
        "class": "read",
        "requires_module": "account",
    },
    "list_customer_recent_activity": {
        "schema": LIST_RECENT_ACTIVITY_SCHEMA,
        "callable": list_customer_recent_activity,
        "class": "read",
    },
}
