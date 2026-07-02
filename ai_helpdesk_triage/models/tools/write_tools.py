"""Write tools — real side effects, gated behind autonomy_level=full."""

from __future__ import annotations

from markupsafe import Markup, escape

from .reputation_tools import DENYLISTED_DOMAINS

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
        "PRIMARY tool for password / login-access requests. Triggers Odoo's "
        "standard auth_signup reset flow: the customer receives a signed, "
        "time-limited link they use themselves to set a new password. This "
        "tool never sets or exposes a password, never bypasses email "
        "verification, and never grants access without the customer's own "
        "action. Call this immediately when the ticket asks for a password "
        "reset, activation email, or first-login setup. Do NOT escalate a "
        "password-reset ask — it is exactly what this tool is for."
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

UPDATE_CUSTOMER_CONTACT_SCHEMA = {
    "name": "update_customer_contact",
    "description": (
        "Update the customer's stored email or phone on their partner "
        "record. Use only when the ticket explicitly asks to correct or "
        "change contact info, and only after lookup_customer has confirmed "
        "which partner you are editing. Never change contact info to a "
        "value the customer did not themselves supply in the ticket."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "email": {
                "type": "string",
                "description": "New email address, when provided.",
                "minLength": 3,
            },
            "phone": {
                "type": "string",
                "description": "New phone number, when provided.",
                "minLength": 3,
            },
        },
        "required": [],
    },
}

POST_CUSTOMER_REPLY_SCHEMA = {
    "name": "post_customer_reply",
    "description": (
        "PRIMARY tool for answering the customer directly. Posts a reply to "
        "the ticket chatter that the customer sees. Call this to (a) confirm "
        "a write action you just took, (b) answer a factual question you can "
        "already answer from the ticket + lookup results, or (c) provide "
        "simple instructions. Do NOT escalate a question you can answer — "
        "use this tool instead."
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
    invoice = (
        env["account.move"]
        .sudo()
        .search(
            [
                ("name", "=", reference),
                ("move_type", "in", ("out_invoice", "out_refund")),
            ],
            limit=1,
        )
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
        user = (
            env["res.users"]
            .sudo()
            .search(
                [("partner_id.email", "=ilike", email)],
                limit=1,
            )
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
    partner_ids = []
    if ticket.partner_id:
        partner_ids.append(ticket.partner_id.id)
    elif ticket.partner_email:
        partner = env["res.partner"].sudo().search([("email", "=ilike", ticket.partner_email)], limit=1)
        if partner:
            partner_ids.append(partner.id)
    if not partner_ids:
        # No linked partner and no partner_email match: the reply would land
        # only in internal chatter, leaving the customer un-notified. Surface
        # this to the agent so it escalates instead of falsely resolving.
        return {"ok": False, "error": "no_recipient"}
    message = Markup("<p>%s</p>") % escape(body)
    ticket.sudo().message_post(
        body=message,
        message_type="comment",
        subtype_xmlid="mail.mt_comment",
        partner_ids=partner_ids,
    )
    return {"ok": True, "data": {"posted": True, "recipients": len(partner_ids)}}


def update_customer_contact(env, ticket, email=None, phone=None):
    """Change the partner's stored email or phone with an audit trail.

    Guards against a prompt-injection account-takeover chain: a malicious
    ticket body could tell the model to rewrite the customer's email and
    then request a password reset — the reset link would land in the
    attacker's inbox. To make that path fail-closed, every new value must
    appear verbatim (case-insensitive) inside ticket.name or
    ticket.description, and email destinations on the reputation
    denylist are refused outright.
    """
    if not ticket.partner_id:
        return {"ok": False, "error": "no_partner"}
    ticket_text = f"{ticket.name or ''}\n{ticket.description or ''}".casefold()
    changes = {}
    if email:
        email = email.strip()
        if not email:
            pass
        elif email.casefold() not in ticket_text:
            return {
                "ok": False,
                "error": "value_not_in_ticket_text",
                "detail": "email must appear verbatim in the ticket subject or description",
            }
        elif "@" in email and email.rsplit("@", 1)[-1].casefold() in DENYLISTED_DOMAINS:
            return {
                "ok": False,
                "error": "denylisted_email_domain",
                "detail": email.rsplit("@", 1)[-1],
            }
        elif email != (ticket.partner_id.email or ""):
            changes["email"] = email
    if phone:
        phone = phone.strip()
        if not phone:
            pass
        elif phone.casefold() not in ticket_text:
            return {
                "ok": False,
                "error": "value_not_in_ticket_text",
                "detail": "phone must appear verbatim in the ticket subject or description",
            }
        elif phone != (ticket.partner_id.phone or ""):
            changes["phone"] = phone
    if not changes:
        return {"ok": True, "data": {"updated": False, "reason": "no_changes"}}
    old = {"email": ticket.partner_id.email or "", "phone": ticket.partner_id.phone or ""}
    ticket.partner_id.sudo().write(changes)
    ticket.sudo().message_post(
        body=Markup("<p>%s</p><ul>%s</ul>")
        % (
            escape("AI updated customer contact info"),
            Markup("").join(
                Markup("<li><code>%s</code>: %s &rarr; %s</li>") % (escape(key), escape(old.get(key, "")), escape(val))
                for key, val in changes.items()
            ),
        ),
        message_type="notification",
        subtype_xmlid="mail.mt_note",
    )
    return {
        "ok": True,
        "data": {"updated": True, "changes": changes},
    }


def _module_installed(env, name):
    return bool(
        env["ir.module.module"].sudo().search_count([("name", "=", name), ("state", "=", "installed")]),
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
    "update_customer_contact": {
        "schema": UPDATE_CUSTOMER_CONTACT_SCHEMA,
        "callable": update_customer_contact,
        "class": "write",
    },
}
