"""Shared Anthropic HTTP client used by the agent loop.

The triage flow keeps its own copy in helpdesk_ticket for backwards compatibility
with existing test mocks; this module exists so the agentic resolution loop can
be tested in isolation without touching the triage code path.
"""

from __future__ import annotations

import time

from odoo import _
from odoo.exceptions import UserError

try:
    import requests
except ImportError:  # pragma: no cover - runtime guard
    requests = None

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS = 1500
REQUEST_TIMEOUT = (5, 60)
MAX_RETRIES = 3
BACKOFF_SECONDS = 1.5
TRANSIENT_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
INPUT_COST_PER_MILLION = 3.00
OUTPUT_COST_PER_MILLION = 15.00


def get_api_key(env):
    """Read the Anthropic API key from ir.config_parameter."""
    return (
        env["ir.config_parameter"]
        .sudo()
        .get_param("ai_helpdesk_triage.anthropic_api_key")
    )


def estimate_cost(input_tokens, output_tokens):
    """Estimate USD cost from Anthropic token counts."""
    input_cost = (input_tokens / 1_000_000) * INPUT_COST_PER_MILLION
    output_cost = (output_tokens / 1_000_000) * OUTPUT_COST_PER_MILLION
    return round(input_cost + output_cost, 6)


def post_message(env, payload):
    """POST to Anthropic with timeouts and exponential backoff.

    Raises UserError on unrecoverable failure. Returns parsed JSON dict.
    """
    if requests is None:
        raise UserError(_("The Python 'requests' library is not installed."))
    api_key = get_api_key(env)
    if not api_key:
        raise UserError(
            _("Configure the Anthropic API key in AI Helpdesk settings."),
        )

    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            response = requests.post(
                ANTHROPIC_API_URL,
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": ANTHROPIC_API_VERSION,
                    "content-type": "application/json",
                },
                json=payload,
                timeout=REQUEST_TIMEOUT,
            )
        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
        ) as exc:
            last_error = exc
            _sleep_before_retry(attempt)
            continue
        except requests.exceptions.RequestException as exc:
            raise UserError(
                _("Could not reach the AI service: %s") % str(exc),
            ) from exc

        if response.status_code in TRANSIENT_STATUS_CODES:
            last_error = UserError(
                _("AI service returned temporary HTTP %(status)s: %(body)s")
                % {
                    "status": response.status_code,
                    "body": _safe_body(response),
                },
            )
            _sleep_before_retry(attempt)
            continue
        if response.status_code >= 400:
            raise UserError(
                _("AI service rejected the request with HTTP %(status)s: %(body)s")
                % {
                    "status": response.status_code,
                    "body": _safe_body(response),
                },
            )
        try:
            return response.json()
        except ValueError as exc:
            raise UserError(_("AI service returned invalid JSON.")) from exc

    raise UserError(_("AI service did not respond after retries: %s") % last_error)


def _sleep_before_retry(attempt):
    """Back off between transient Anthropic failures."""
    if attempt < MAX_RETRIES - 1:
        time.sleep(BACKOFF_SECONDS * (2**attempt))


def _safe_body(response):
    """Return a bounded, sanitized error body."""
    text = getattr(response, "text", "") or ""
    return text.strip()[:500]
