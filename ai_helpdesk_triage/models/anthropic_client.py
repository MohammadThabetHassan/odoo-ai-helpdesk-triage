"""Shared LLM HTTP client used by the agent loop.

Supports two providers:

- ``anthropic`` (default): direct Anthropic Messages API.
- ``bedrock``: Amazon Bedrock hosted Claude models, using Bedrock long-lived
  API keys as Bearer tokens.

Provider selection and credentials come from ``ir.config_parameter`` — nothing
about the key or account touches the source tree.
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
BEDROCK_ANTHROPIC_VERSION = "bedrock-2023-05-31"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-6"
DEFAULT_BEDROCK_MODEL = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
DEFAULT_BEDROCK_REGION = "us-east-1"
DEFAULT_MAX_TOKENS = 1500
REQUEST_TIMEOUT = (5, 60)
MAX_RETRIES = 3
BACKOFF_SECONDS = 1.5
TRANSIENT_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
INPUT_COST_PER_MILLION = 3.00
OUTPUT_COST_PER_MILLION = 15.00


def get_provider(env):
    """Return the active LLM provider (anthropic or bedrock)."""
    value = (
        env["ir.config_parameter"]
        .sudo()
        .get_param("ai_helpdesk_triage.provider", "anthropic")
    )
    return value if value in ("anthropic", "bedrock") else "anthropic"


def get_api_key(env):
    """Return the credential for the active provider."""
    provider = get_provider(env)
    icp = env["ir.config_parameter"].sudo()
    if provider == "bedrock":
        return icp.get_param("ai_helpdesk_triage.bedrock_api_key")
    return icp.get_param("ai_helpdesk_triage.anthropic_api_key")


def get_bedrock_config(env):
    """Return the Bedrock region and model ID from settings."""
    icp = env["ir.config_parameter"].sudo()
    region = icp.get_param(
        "ai_helpdesk_triage.bedrock_region", DEFAULT_BEDROCK_REGION,
    )
    model_id = icp.get_param(
        "ai_helpdesk_triage.bedrock_model_id", DEFAULT_BEDROCK_MODEL,
    )
    return region or DEFAULT_BEDROCK_REGION, model_id or DEFAULT_BEDROCK_MODEL


def estimate_cost(input_tokens, output_tokens):
    """Estimate USD cost from token counts (Sonnet pricing baseline)."""
    input_cost = (input_tokens / 1_000_000) * INPUT_COST_PER_MILLION
    output_cost = (output_tokens / 1_000_000) * OUTPUT_COST_PER_MILLION
    return round(input_cost + output_cost, 6)


def post_message(env, payload):
    """POST a Messages-compatible payload and return the parsed response.

    ``payload`` uses the direct Anthropic schema. For Bedrock, the ``model``
    key is moved to the URL and ``anthropic_version`` is injected into the
    body — the caller does not need to know which provider is active.
    """
    if requests is None:
        raise UserError(_("The Python 'requests' library is not installed."))

    provider = get_provider(env)
    api_key = get_api_key(env)
    if not api_key:
        raise UserError(
            _("Configure the AI provider credentials in AI Helpdesk settings."),
        )

    url, headers, body = _prepare_request(env, provider, api_key, payload)

    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            response = requests.post(
                url,
                headers=headers,
                json=body,
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


def _prepare_request(env, provider, api_key, payload):
    """Return (url, headers, body) for the active provider."""
    body = dict(payload)
    if provider == "bedrock":
        region, model_id = get_bedrock_config(env)
        # Bedrock takes the model in the URL; the body carries anthropic_version.
        body.pop("model", None)
        body.setdefault("anthropic_version", BEDROCK_ANTHROPIC_VERSION)
        url = (
            f"https://bedrock-runtime.{region}.amazonaws.com/"
            f"model/{model_id}/invoke"
        )
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        return url, headers, body

    body.setdefault("model", DEFAULT_ANTHROPIC_MODEL)
    return (
        ANTHROPIC_API_URL,
        {
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_API_VERSION,
            "content-type": "application/json",
        },
        body,
    )


def _sleep_before_retry(attempt):
    """Back off between transient failures."""
    if attempt < MAX_RETRIES - 1:
        time.sleep(BACKOFF_SECONDS * (2**attempt))


def _safe_body(response):
    """Return a bounded, sanitized error body."""
    text = getattr(response, "text", "") or ""
    return text.strip()[:500]
