"""Show what the AI agent's tools have queued in Odoo's outgoing mail.

Prints the last N mail.mail rows plus (optionally) a rendered HTML file
you can open in a browser to see exactly what the customer would have
received. Handy after a demo pass to verify send_password_reset,
resend_invoice_pdf, or post_customer_reply landed the right content.

Usage:

    # summary of the last 5 outgoing mails
    python ai_helpdesk_triage/scripts/show_agent_emails.py

    # last 20
    python ai_helpdesk_triage/scripts/show_agent_emails.py --limit 20

    # also render an HTML file you can open in a browser
    python ai_helpdesk_triage/scripts/show_agent_emails.py --html

    # point at a different DB / URL
    python ai_helpdesk_triage/scripts/show_agent_emails.py \
        --url http://127.0.0.1:8069 --db myapp --login admin --password admin

Failure states:

    state=exception    SMTP is not configured (or rejected the send). The
                       body_html is still stored — the mail queued but
                       never left the machine, which is what you want for
                       a demo.
    state=sent         Actually delivered to the SMTP server.
    state=outgoing     Waiting for the mail cron to pick it up.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys
import xmlrpc.client


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", default="http://127.0.0.1:8069")
    parser.add_argument("--db", default="myapp")
    parser.add_argument("--login", default="admin")
    parser.add_argument("--password", default="admin")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument(
        "--html",
        action="store_true",
        help="Also render a browsable HTML file to ./agent-emails.html.",
    )
    parser.add_argument(
        "--out",
        default="agent-emails.html",
        help="Output HTML path when --html is used.",
    )
    args = parser.parse_args()

    common = xmlrpc.client.ServerProxy(f"{args.url}/xmlrpc/2/common")
    uid = common.authenticate(args.db, args.login, args.password, {})
    if not uid:
        print("Auth failed. Check --db / --login / --password.", file=sys.stderr)
        return 2
    models = xmlrpc.client.ServerProxy(f"{args.url}/xmlrpc/2/object")

    fields = [
        "id",
        "subject",
        "email_from",
        "email_to",
        "recipient_ids",
        "state",
        "failure_reason",
        "body_html",
        "date",
    ]
    mails = models.execute_kw(
        args.db,
        uid,
        args.password,
        "mail.mail",
        "search_read",
        [[]],
        {"fields": fields, "order": "id desc", "limit": args.limit},
    )

    if not mails:
        print("mail.mail queue is empty.")
        return 0

    print(f"=== mail.mail (last {len(mails)}) ===")
    for m in mails:
        recipient = m.get("email_to") or m.get("recipient_ids") or "(no recipient)"
        state = m["state"]
        marker = {"sent": "OK", "exception": "FAIL", "outgoing": "PENDING"}.get(state, state.upper())
        print(f"  #{m['id']} [{marker}] to={recipient} | {m['subject']!r}")
        if m.get("failure_reason"):
            reason = m["failure_reason"].splitlines()[0]
            print(f"      failure: {reason}")

    if args.html:
        _render_html(mails, args.out)
        print()
        print(f"HTML rendered to: {pathlib.Path(args.out).resolve()}")

    return 0


def _render_html(mails, out_path: str) -> None:
    parts = [
        "<!DOCTYPE html>",
        "<html><head><meta charset='utf-8'><title>AI Helpdesk — Agent Emails</title>",
        "<style>",
        "body{font-family:system-ui;max-width:900px;margin:2em auto;padding:0 1em;color:#222}",
        ".mail{border:1px solid #ccc;padding:1em;margin-bottom:2em;border-radius:8px;background:#fafafa}",
        ".meta{color:#555;font-size:.9em;margin-bottom:.5em}",
        ".state-sent{color:#0a0}.state-exception{color:#c00}.state-outgoing{color:#a80}",
        "hr{border:0;border-top:1px solid #ddd;margin:1em 0}",
        "</style></head><body>",
        f"<h1>Odoo mail.mail queue ({len(mails)} rows)</h1>",
    ]
    for m in mails:
        recipient = m.get("email_to") or m.get("recipient_ids") or "(no recipient)"
        body = m.get("body_html") or "<i>(empty body)</i>"
        parts.append('<div class="mail">')
        subject = (m.get("subject") or "(no subject)").replace("<", "&lt;").replace(">", "&gt;")
        parts.append(f"<h2>#{m['id']} — {subject}</h2>")
        parts.append(
            f'<div class="meta">from <b>{m["email_from"]}</b> → to <b>{recipient}</b>'
            f' | state=<span class="state-{m["state"]}">{m["state"]}</span>'
            f" | date={m['date']}</div>"
        )
        if m.get("failure_reason"):
            reason = m["failure_reason"].splitlines()[0]
            parts.append(f'<div class="meta">failure: {reason}</div>')
        parts.append("<hr>")
        parts.append(body)
        parts.append("</div>")
    parts.append("</body></html>")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    pathlib.Path(out_path).write_text("\n".join(parts), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
