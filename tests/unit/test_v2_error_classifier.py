"""B-227 + P0-2: ErrorClassifier unit tests.

Pin the 7-stage classification pipeline so refactors don't accidentally
re-classify rate_limit as auth (or vice versa). Each test mocks just
enough of an exception to trigger ONE branch — covering all 12 reasons
plus the disambiguation rules (402 transient quota → rate_limit, 400
generic + large session → context_overflow).
"""
from __future__ import annotations

from xmclaw.utils.error_classifier import (
    ClassifiedError,
    FailoverReason,
    backoff_schedule,
    classify_api_error,
)


class _FakeError(Exception):
    """Mimics openai/anthropic APIError shape: optional ``.status_code`` +
    optional ``.body`` dict + optional ``.response`` adapter — just enough
    for the extractor."""

    def __init__(
        self, msg: str,
        status_code: int | None = None,
        body: dict | None = None,
    ) -> None:
        super().__init__(msg)
        self.status_code = status_code
        self.body = body


# ── Status-code branch ───────────────────────────────────────────────


def test_status_429_is_rate_limit() -> None:
    ce = classify_api_error(_FakeError("rate limited", status_code=429))
    assert ce.reason == FailoverReason.rate_limit
    assert ce.retryable
    assert ce.should_rotate_credential
    assert ce.should_fallback
    assert not ce.should_compress


def test_status_503_is_overloaded() -> None:
    ce = classify_api_error(_FakeError("service unavailable", status_code=503))
    assert ce.reason == FailoverReason.overloaded
    assert ce.retryable


def test_status_529_is_overloaded() -> None:
    """Anthropic-specific overloaded status code."""
    ce = classify_api_error(_FakeError("overloaded", status_code=529))
    assert ce.reason == FailoverReason.overloaded


def test_status_500_is_server_error() -> None:
    ce = classify_api_error(_FakeError("internal server error", status_code=500))
    assert ce.reason == FailoverReason.server_error
    assert ce.retryable


def test_status_502_is_server_error() -> None:
    ce = classify_api_error(_FakeError("bad gateway", status_code=502))
    assert ce.reason == FailoverReason.server_error


def test_status_401_is_auth_not_retryable() -> None:
    ce = classify_api_error(_FakeError("invalid key", status_code=401))
    assert ce.reason == FailoverReason.auth
    assert not ce.retryable
    assert ce.should_rotate_credential
    assert ce.should_fallback


def test_status_403_is_auth() -> None:
    ce = classify_api_error(_FakeError("forbidden", status_code=403))
    assert ce.reason == FailoverReason.auth
    assert not ce.retryable
    assert ce.should_fallback


def test_status_401_account_disabled_is_auth_permanent() -> None:
    """401 + 'account disabled' → auth_permanent + should_terminate.
    Rotating creds won't help — the account itself is dead."""
    ce = classify_api_error(_FakeError(
        "account disabled by an administrator",
        status_code=401,
    ))
    assert ce.reason == FailoverReason.auth_permanent
    assert ce.should_terminate
    assert not ce.retryable


def test_msg_pattern_api_key_revoked_is_auth_permanent() -> None:
    """No status code, but message says 'api key revoked' → permanent."""
    ce = classify_api_error(_FakeError(
        "API key has been revoked, contact support",
    ))
    assert ce.reason == FailoverReason.auth_permanent
    assert ce.should_terminate


def test_status_403_key_limit_is_billing() -> None:
    """OpenRouter's 403 'key limit exceeded' is billing, not plain auth."""
    ce = classify_api_error(_FakeError(
        "key limit exceeded for this account", status_code=403,
    ))
    assert ce.reason == FailoverReason.billing
    assert not ce.retryable


def test_status_402_is_billing() -> None:
    ce = classify_api_error(_FakeError("payment required", status_code=402))
    assert ce.reason == FailoverReason.billing
    assert not ce.retryable
    assert ce.should_rotate_credential


def test_status_402_with_transient_signal_is_rate_limit() -> None:
    """402 with 'try again in N minutes' is a transient quota, not billing."""
    ce = classify_api_error(_FakeError(
        "Usage limit exceeded, try again in 5 minutes", status_code=402,
    ))
    assert ce.reason == FailoverReason.rate_limit
    assert ce.retryable


def test_status_413_is_payload_too_large_with_compress() -> None:
    """413 → payload_too_large; compress + retry."""
    ce = classify_api_error(_FakeError("too large", status_code=413))
    assert ce.reason == FailoverReason.payload_too_large
    assert ce.should_compress
    assert ce.retryable


def test_status_404_with_model_pattern_is_model_not_found() -> None:
    ce = classify_api_error(_FakeError(
        "model 'kimi-k99' does not exist", status_code=404,
    ))
    assert ce.reason == FailoverReason.model_not_found
    assert not ce.retryable
    assert ce.should_fallback


def test_status_404_generic_is_unknown() -> None:
    """Generic 404 (wrong endpoint path) shouldn't claim model_not_found."""
    ce = classify_api_error(_FakeError("Not found", status_code=404))
    assert ce.reason == FailoverReason.unknown
    assert ce.retryable


def test_status_400_with_context_pattern_is_context_overflow() -> None:
    ce = classify_api_error(_FakeError(
        "context length exceeded the limit", status_code=400,
    ))
    assert ce.reason == FailoverReason.context_overflow
    assert ce.should_compress
    assert ce.retryable


def test_status_400_generic_large_session_is_context_overflow() -> None:
    """Anthropic returns bare 'Error' on context overflow — disambiguate
    by session size (approx_tokens > 80K or num_messages > 80)."""
    ce = classify_api_error(
        _FakeError("Error", status_code=400),
        approx_tokens=100_000,
        num_messages=120,
    )
    assert ce.reason == FailoverReason.context_overflow
    assert ce.should_compress


def test_status_400_generic_small_session_is_format_error() -> None:
    """Generic 400 on small session is a real format_error."""
    ce = classify_api_error(
        _FakeError("Bad request", status_code=400),
        approx_tokens=1000, num_messages=3,
    )
    assert ce.reason == FailoverReason.format_error
    assert not ce.retryable


def test_other_5xx_is_server_error() -> None:
    """504 / 507 etc — generic 5xx still retryable."""
    ce = classify_api_error(_FakeError("gateway timeout", status_code=504))
    assert ce.reason == FailoverReason.server_error
    assert ce.retryable


def test_other_4xx_is_format_error() -> None:
    """410 / 422 etc — non-retryable format_error."""
    ce = classify_api_error(_FakeError("unprocessable", status_code=422))
    assert ce.reason == FailoverReason.format_error
    assert not ce.retryable


# ── Provider-specific patterns (priority 1) ──────────────────────────


def test_anthropic_thinking_signature_400() -> None:
    """400 + 'signature' + 'thinking' → thinking_signature, retry once."""
    ce = classify_api_error(_FakeError(
        "thinking block signature is invalid", status_code=400,
    ))
    assert ce.reason == FailoverReason.thinking_signature
    assert ce.retryable
    assert not ce.should_compress


def test_anthropic_long_context_tier_429() -> None:
    """429 + 'extra usage' + 'long context' → long_context_tier, compress."""
    ce = classify_api_error(_FakeError(
        "extra usage required for long context tier", status_code=429,
    ))
    assert ce.reason == FailoverReason.long_context_tier
    assert ce.should_compress
    assert ce.retryable


# ── Error-code branch (body.error.code) ──────────────────────────────


def test_error_code_resource_exhausted() -> None:
    err = _FakeError(
        "Provider error",
        body={"error": {"code": "resource_exhausted", "message": "out of tokens"}},
    )
    ce = classify_api_error(err)
    assert ce.reason == FailoverReason.rate_limit


def test_error_code_insufficient_quota() -> None:
    err = _FakeError(
        "Provider error",
        body={"error": {"code": "insufficient_quota", "message": "out of credit"}},
    )
    ce = classify_api_error(err)
    assert ce.reason == FailoverReason.billing
    assert not ce.retryable


def test_error_code_context_length_exceeded() -> None:
    err = _FakeError(
        "Provider error",
        body={"error": {"code": "context_length_exceeded"}},
    )
    ce = classify_api_error(err)
    assert ce.reason == FailoverReason.context_overflow
    assert ce.should_compress


def test_error_code_model_not_found() -> None:
    err = _FakeError(
        "Provider error",
        body={"error": {"code": "model_not_found"}},
    )
    ce = classify_api_error(err)
    assert ce.reason == FailoverReason.model_not_found
    assert ce.should_fallback


# ── Message-pattern branch (no status code) ─────────────────────────


def test_context_overflow_beats_rate_limit_when_both_in_msg() -> None:
    """When status code is absent but msg has context patterns, context
    wins over rate_limit (both can mention 'limit')."""
    ce = classify_api_error(_FakeError(
        "context length exceeded the limit",
    ))
    assert ce.reason == FailoverReason.context_overflow
    assert ce.should_compress


def test_chinese_context_overflow_pattern() -> None:
    ce = classify_api_error(_FakeError("请求失败: 上下文长度过长"))
    assert ce.reason == FailoverReason.context_overflow
    assert ce.should_compress


def test_rate_limit_msg_pattern() -> None:
    ce = classify_api_error(_FakeError(
        "rate limit exceeded, please retry after 5s",
    ))
    assert ce.reason == FailoverReason.rate_limit


def test_too_many_requests_msg() -> None:
    ce = classify_api_error(_FakeError("Too many requests"))
    assert ce.reason == FailoverReason.rate_limit


def test_billing_pattern_in_msg() -> None:
    ce = classify_api_error(_FakeError(
        "Your credit balance has been exhausted",
    ))
    assert ce.reason == FailoverReason.billing
    assert not ce.retryable


def test_payload_too_large_pattern_in_msg() -> None:
    """Some proxies embed the status in the msg without a .status_code attr."""
    ce = classify_api_error(_FakeError(
        "Provider returned: error code: 413 request entity too large",
    ))
    assert ce.reason == FailoverReason.payload_too_large
    assert ce.should_compress


def test_usage_limit_with_transient_signal_is_rate_limit() -> None:
    ce = classify_api_error(_FakeError(
        "Usage limit reached, try again at 12:00",
    ))
    assert ce.reason == FailoverReason.rate_limit
    assert ce.retryable


def test_usage_limit_without_transient_is_billing() -> None:
    ce = classify_api_error(_FakeError("Quota exceeded — top up"))
    assert ce.reason == FailoverReason.billing
    assert not ce.retryable


def test_auth_msg_pattern() -> None:
    ce = classify_api_error(_FakeError("Invalid API key supplied"))
    assert ce.reason == FailoverReason.auth
    assert not ce.retryable
    assert ce.should_rotate_credential


def test_model_not_found_msg_pattern() -> None:
    ce = classify_api_error(_FakeError(
        "is not a valid model — try another one",
    ))
    assert ce.reason == FailoverReason.model_not_found


def test_timeout_error_class_directly() -> None:
    """A bare TimeoutError (no msg pattern) still classifies."""
    ce = classify_api_error(TimeoutError())
    assert ce.reason == FailoverReason.timeout


def test_oserror_classifies_as_timeout() -> None:
    """Connection-level OSError is treated as transport timeout."""
    ce = classify_api_error(OSError("connection refused"))
    assert ce.reason == FailoverReason.timeout


def test_unknown_fallback() -> None:
    ce = classify_api_error(_FakeError("some weird gibberish"))
    assert ce.reason == FailoverReason.unknown
    assert ce.retryable


# ── Server-disconnect heuristic ──────────────────────────────────────


def test_server_disconnect_large_session_is_context_overflow() -> None:
    ce = classify_api_error(
        _FakeError("server disconnected without sending a response"),
        approx_tokens=150_000,
    )
    assert ce.reason == FailoverReason.context_overflow
    assert ce.should_compress


def test_server_disconnect_small_session_is_timeout() -> None:
    ce = classify_api_error(
        _FakeError("server disconnected"),
        approx_tokens=5_000,
    )
    assert ce.reason == FailoverReason.timeout


# ── OpenRouter wrapped error metadata ────────────────────────────────


def test_openrouter_wrapped_metadata_raw_context_overflow() -> None:
    """OpenRouter wraps upstream errors in error.metadata.raw — the inner
    JSON contains the real error message."""
    err = _FakeError(
        "Provider returned error",
        body={
            "error": {
                "message": "Provider returned error",
                "metadata": {
                    "raw": '{"error": {"message": "context length '
                           'exceeded the maximum"}}',
                },
            },
        },
    )
    ce = classify_api_error(err)
    assert ce.reason == FailoverReason.context_overflow
    assert ce.should_compress


# ── Body-message extraction ─────────────────────────────────────────


def test_pattern_in_body_message() -> None:
    """SDK exceptions with body but bare str() — must still match."""
    err = _FakeError(
        "Provider error",
        status_code=400,
        body={"error": {"message": "context window too small"}},
    )
    ce = classify_api_error(err)
    assert ce.reason == FailoverReason.context_overflow


# ── Backoff schedule ────────────────────────────────────────────────


def test_backoff_schedule_for_each_reason() -> None:
    """Every retryable reason has at least one retry; never-retry reasons
    have empty schedule; ascending values for backoff-style reasons."""
    rl = backoff_schedule(FailoverReason.rate_limit)
    ov = backoff_schedule(FailoverReason.overloaded)
    se = backoff_schedule(FailoverReason.server_error)
    to = backoff_schedule(FailoverReason.timeout)
    co = backoff_schedule(FailoverReason.context_overflow)
    pt = backoff_schedule(FailoverReason.payload_too_large)
    lct = backoff_schedule(FailoverReason.long_context_tier)
    ts = backoff_schedule(FailoverReason.thinking_signature)
    auth = backoff_schedule(FailoverReason.auth)
    auth_p = backoff_schedule(FailoverReason.auth_permanent)
    bill = backoff_schedule(FailoverReason.billing)
    mnf = backoff_schedule(FailoverReason.model_not_found)
    fmt = backoff_schedule(FailoverReason.format_error)
    unk = backoff_schedule(FailoverReason.unknown)

    # Retryable reasons have at least 1 attempt
    assert len(rl) >= 2
    assert len(ov) >= 2
    assert len(se) >= 1
    assert len(to) >= 1
    assert len(co) >= 1
    assert len(pt) >= 1
    assert len(lct) >= 1
    assert len(ts) >= 1
    assert len(unk) >= 1

    # Never-retry reasons have empty schedule
    assert auth == ()
    assert auth_p == ()
    assert bill == ()
    assert mnf == ()
    assert fmt == ()

    # Ascending (each retry waits longer) for backoff-style reasons
    for sched in (rl, ov, se, to):
        assert sorted(sched) == list(sched)


def test_classified_error_dataclass_defaults() -> None:
    """Defensive: bare ClassifiedError still has sane defaults."""
    ce = ClassifiedError(reason=FailoverReason.unknown)
    assert ce.retryable
    assert not ce.should_compress
    assert not ce.should_rotate_credential
    assert not ce.should_fallback
    assert ce.status_code is None


def test_is_auth_property() -> None:
    """The convenience property covers both auth + auth_permanent."""
    assert ClassifiedError(reason=FailoverReason.auth).is_auth
    assert ClassifiedError(reason=FailoverReason.auth_permanent).is_auth
    assert not ClassifiedError(reason=FailoverReason.rate_limit).is_auth
