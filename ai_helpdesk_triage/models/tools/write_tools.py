"""Write tools — real side effects, gated behind autonomy_level=full."""

from __future__ import annotations

from markupsafe import Markup, escape


RESEND_INVOICE_SCHEMA = {
    "name": "resend_invoice_pdf",
    "description": (
        "Regenerate the invoice PDF and email it to the customer immediately. "
        "Only works if the accounting module is installed. Use this when a "
        "customer reports never receiving their invoice."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "reference": {"type": "string", "minLength": 3},
        },
        "required": ["reference"],
    },
}

SEND_PASSWORD_RESET_SCHEMA = {
    "name": "send_password_reset",
    "description": (
        "Send a password-reset link to the given customer email. Does NOT set a "
        "new password. Use this for login issues where the customer needs to "
        "regain access."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "email": {"type": "string", "minLength": 3},
        },
        "required": ["email"],
    },
}

POST_CUSTOMER_REPLY_SCHEMA = {
    "name": "post_customer_reply",
    "description": (
        "Post a reply message to the ticket's chatter, visible to the customer. "
        "Use this to confirm actions taken or provide instructions."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "body": {
                "type": "string",
                "description": "The reply body in plain text.",
                "minLength": 1,
                "maxLength": 4000,
            },
        },
        "required": ["body"],
    },
}


def resend_invoice_pdf(env, ticket, reference):
    """Regenerate and email an invoice PDF to its partner."""
    if not _module_installed(env, "account"):
        return {"ok": False, "error": "module_not_installed", "module": "account"}
    invoice = env["account.move"].sudo().search(
        [("name", "=", reference), ("move_type", "in", ("out_invoice", "out_refund"))],
        limit=1,
    )
    if not invoice:
        return {"ok": False, "error": "invoice_not_found", "reference": reference}
    if invoice.state != "posted":
        return {
            "ok": False,
            "error": "invoice_not_posted",
            "state": invoice.state,
        }
    template = env.ref(
        "account.email_template_edi_invoice",
        raise_if_not_found=False,
    )
    if not template:
        return {"ok": False, "error": "invoice_template_missing"}
    try:
        template.sudo().send_mail(invoice.id, force_send=True)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "send_failed", "detail": str(exc)}
    return {
        "ok": True,
        "data": {
            "reference": invoice.name,
            "recipient": invoice.partner_id.email or "",
        },
    }


def send_password_reset(env, ticket, email):
    """Send an Odoo password reset link to the target user."""
    if not _module_installed(env, "auth_signup"):
        return {"ok": False, "error": "module_not_installed", "module": "auth_signup"}
    user = env["res.users"].sudo().search([("login", "=", email)], limit=1)
    if not user:
        user = env["res.users"].sudo().search(
            [("partner_id.email", "=ilike", email)],
            limit=1,
        )
    if not user:
        return {"ok": False, "error": "user_not_found", "email": email}
    try:
        user.action_reset_password()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "reset_failed", "detail": str(exc)}
    return {"ok": True, "data": {"login": user.login, "user_id": user.id}}


def post_customer_reply(env, ticket, body):
    """Post a customer-facing reply on the ticket chatter."""
    partner_ids = [ticket.partner_id.id] if ticket.partner_id else []
    message = Markup("<p>%s</p>") % escape(body)
    ticket.sudo().message_post(
        body=message,
        message_type="comment",
        subtype_xmlid="mail.mt_comment",
        partner_ids=partner_ids,
    )
    return {"ok": True, "data": {"posted": True, "recipients": len(partner_ids)}}


def _module_installed(env, name):
    return bool(
        env["ir.module.module"]
        .sudo()
        .search_count([("name", "=", name), ("state", "=", "installed")]),
    )


WRITE_TOOLS = {
    "resend_invoice_pdf": {
        "schema": RESEND_INVOICE_SCHEMA,
        "callable": resend_invoice_pdf,
        "class": "write",
        "requires_module": "account",
    },
    "send_password_reset": {
        "schema": SEND_PASSWORD_RESET_SCHEMA,
        "callable": send_password_reset,
        "class": "write",
        "requires_module": "auth_signup",
    },
    "post_customer_reply": {
        "schema": POST_CUSTOMER_REPLY_SCHEMA,
        "callable": post_customer_reply,
        "class": "write",
    },
}
