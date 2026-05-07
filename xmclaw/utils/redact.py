"""Secrets scrubber for event payloads.

Every ``BehavioralEvent`` payload passes through ``redact()`` before being
persisted. Conformance test ensures no known secret pattern slips through.

B-246 expands the original 5-pattern catalogue (Phase 1 stub) to cover
the secret types XMclaw users actually paste / configure / leak in the
wild. Pattern selection follows the secret-scanning denylists from
GitHub / GitLab / TruffleHog with an XMclaw-relevant subset:

  * Original 5: OpenAI ``sk-...`` / Anthropic ``sk-ant-...`` /
    Slack ``xox[abprs]-...`` / GitHub ``gh[pousr]_...`` / Google API
    ``AIza...``
  * AWS:        ``AKIA...`` access-key id (we don't try to match the
    secret-key 40-char body — too high false-positive rate)
  * Stripe:     ``sk_live_...`` / ``sk_test_...`` / ``rk_live_...``
                / ``pk_live_...``
  * Anthropic:  ``sk-ant-admin-...`` (admin tier, distinct from sk-ant-)
  * OpenRouter: ``sk-or-v1-...``
  * DeepSeek:   ``sk-ds-...``
  * OpenAI org: ``org-...`` (organisation id; not a secret strictly,
                but reveals tenant info)
  * Discord:    bot tokens ``\\d+\\.\\w{6}\\.\\w{27}`` (3-segment dot-format)
  * Stable:     PEM private-key block headers (``-----BEGIN ...
                PRIVATE KEY-----``)
  * GCP svc:    service-account JSON keys (``"private_key":
                "-----BEGIN``) — handled by the PEM pattern
  * JWT:        triple-segment ``eyJ...\\.eyJ...\\.[A-Za-z0-9_-]+``

Each pattern compiles once at import time. The hot path
(``redact_string``) is a single-pass replace per pattern; for the
~15 patterns the cost stays sub-millisecond on typical event payloads.
"""
from __future__ import annotations

import re
from typing import Any

_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # ─── LLM provider keys ──────────────────────────────────────────
    # Anthropic admin tier (sk-ant-admin-...) MUST come BEFORE the
    # generic sk-ant-... rule, otherwise the latter swallows it.
    (re.compile(r"sk-ant-admin-[A-Za-z0-9_\-]{20,}"), "sk-ant-admin-***"),
    (re.compile(r"sk-ant-api[0-9]{2}-[A-Za-z0-9_\-]{20,}"), "sk-ant-api***"),
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"), "sk-ant-***"),
    # OpenRouter explicit prefix (sk-or-v1-...) before generic sk-...
    (re.compile(r"sk-or-v[0-9]+-[A-Za-z0-9_\-]{20,}"), "sk-or-***"),
    # DeepSeek
    (re.compile(r"sk-ds-[A-Za-z0-9_\-]{20,}"), "sk-ds-***"),
    # Generic OpenAI sk-... (must come AFTER all sk-{vendor}- variants).
    (re.compile(r"sk-[A-Za-z0-9_\-]{20,}"), "sk-***"),
    # OpenAI organisation id
    (re.compile(r"\borg-[A-Za-z0-9]{20,}\b"), "org-***"),
    # Google AI Studio + Gemini API keys
    (re.compile(r"AIza[0-9A-Za-z\-_]{35}"), "AIza***"),

    # ─── Cloud / payments ───────────────────────────────────────────
    # AWS access-key id (the 20-char prefix; matching the 40-char
    # secret-key body is too FP-prone without context)
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIA***"),
    # Stripe (live + test, both kinds)
    (re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b"),
     "stripe_***"),

    # ─── Communication platforms ────────────────────────────────────
    # Slack tokens (xoxa-/xoxb-/xoxp-/xoxr-/xoxs-)
    (re.compile(r"xox[abprs]-[A-Za-z0-9\-]{10,}"), "xox*-***"),
    # GitHub PAT / app / OAuth / refresh tokens
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "gh*_***"),
    # Discord bot token (3-segment dot-separated)
    (re.compile(
        r"\b[MN][A-Za-z\d]{23,25}\.[A-Za-z\d_-]{6,7}\.[A-Za-z\d_-]{27,38}\b"
    ), "discord_***"),

    # ─── Generic high-confidence ───────────────────────────────────
    # PEM private key block (covers RSA / DSA / EC / OPENSSH / generic
    # PRIVATE KEY) — match the BEGIN line through the END line as a
    # single dotall span.
    (re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
    ), "[PEM_PRIVATE_KEY_REDACTED]"),
    # JWT (3 base64url segments, dot-separated). Length-bounded to
    # avoid false-positive on arbitrary base64 substrings.
    (re.compile(
        r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
    ), "[JWT_REDACTED]"),
)


def redact_string(text: str) -> str:
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact(obj: Any) -> Any:  # noqa: ANN401
    if isinstance(obj, str):
        return redact_string(obj)
    if isinstance(obj, dict):
        return {k: redact(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(redact(v) for v in obj)
    return obj
