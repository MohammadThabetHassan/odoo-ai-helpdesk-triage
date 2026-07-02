"""Read-only domain reputation lookup tools for the agent."""

from __future__ import annotations

CHECK_DOMAIN_REPUTATION_SCHEMA = {
    "name": "check_domain_reputation",
    "description": (
        "Check whether an email domain looks trusted, suspicious, or unknown. "
        "Use this when a ticket comes from an unfamiliar sender and you need a "
        "deterministic reputation hint before deciding what to do next. "
        "Trusted includes common mailbox providers and known customer company "
        "domains. Suspicious includes known throwaway email providers. "
        "Anything else returns unknown."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "domain": {
                "type": "string",
                "description": "The email domain to check, such as gmail.com.",
                "minLength": 1,
            },
        },
        "required": ["domain"],
    },
}


DENYLISTED_DOMAINS = {
    "10minutemail.com",
    "guerrillamail.com",
    "mailinator.com",
    "temp-mail.org",
    "tempmail.com",
    "yopmail.com",
}


TRUSTED_DOMAINS = {
    "gmail.com",
    "outlook.com",
}


def check_domain_reputation(env, ticket, domain):
    """Return a simple deterministic verdict for an email domain."""
    normalized = (domain or "").strip().lower()

    if not normalized or "." not in normalized:
        return {
            "ok": True,
            "data": {
                "domain": normalized,
                "verdict": "unknown",
            },
        }

    if normalized in DENYLISTED_DOMAINS:
        verdict = "suspicious"
    elif normalized in _get_trusted_domains(env):
        verdict = "trusted"
    else:
        verdict = "unknown"

    return {
        "ok": True,
        "data": {
            "domain": normalized,
            "verdict": verdict,
        },
    }


def _get_trusted_domains(env):
    """Return the static allowlist plus domains found on partner emails/websites."""
    domains = set(TRUSTED_DOMAINS)

    partners = env["res.partner"].sudo().search([])
    for partner in partners:
        email_domain = _extract_domain(partner.email)
        if email_domain:
            domains.add(email_domain)

        website_domain = _extract_website_domain(partner.website)
        if website_domain:
            domains.add(website_domain)

    return domains


def _extract_domain(email):
    """Extract the domain part from an email address."""
    if not email or "@" not in email:
        return False
    domain = email.split("@")[-1].strip().lower()
    return domain if "." in domain else False


def _extract_website_domain(website):
    """Extract a hostname-like domain from a website field."""
    if not website:
        return False

    value = website.strip().lower()
    value = value.replace("https://", "").replace("http://", "")
    value = value.split("/")[0]
    value = value.replace("www.", "")

    return value if "." in value else False


REPUTATION_TOOLS = {
    "check_domain_reputation": {
        "schema": CHECK_DOMAIN_REPUTATION_SCHEMA,
        "callable": check_domain_reputation,
        "class": "read",
    },
}
