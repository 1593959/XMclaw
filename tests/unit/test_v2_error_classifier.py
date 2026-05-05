"""B-227: ErrorClassifier unit tests.

Pin the classification pipeline so a refactor doesn't accidentally
re-classify rate_limit as auth (or vice versa). Each test mocks
just enough of an exception to trigger ONE branch.
"""
from __future__ import annotations

from xmclaw.utils.error_classifier import (
    ClassifiedError,
    FailoverReason,
    backoff_schedule,
    classify_api_error,
)


class _FakeError(Exception):
    """Mimics openai/anthropic APIError shape: optional .status_code +
    optional .body dict. Just enough for the extractor."""

    def __init__(self, msg: str, status_code: int | None = None,
                 body: dict | None = None) -> None:
        super().__init__(msg)
        self.status_code = status_code
        self.body = body


# ── Status-code branch ──────────────────────────────────────────────


def test_status_429_is_rate_limit() -> None:
    ce = classify_api_error(_FakeError("rate limited", status_code=429))
    assert ce.reason == FailoverReason.rate_limit
    assert ce.retryable
    assert not ce.should_compress


def test_status_503_is_overloaded() -> None:
    ce = classify_api_error(_FakeError("service unavailable", status_code=503))
    assert ce.reason == FailoverReason.overloaded
    assert ce.retryable


def test_status_529_is_overloaded() -> None:
    """Anthropic-specific overloaded status code."""
    ce = classify_api_error(_FakeError("overloaded", status_code=529))
    assert ce.reason == FailoverReason.overloaded


def test_status_401_is_auth_not_retryable() -> None:
    ce = classify_api_error(_FakeError("invalid key", status_code=401))
    assert ce.reason == FailoverReason.auth
    assert not ce.retryable


def test_status_403_is_auth() -> None:
    ce = classify_api_error(_FakeError("forbidden", status_code=403))
    assert ce.reason == FailoverReason.auth


def test_status_413_is_context_overflow_with_compress() -> None:
    """Payload too large → compress and retry."""
    ce = classify_api_error(_FakeError("too large", status_code=413))
    assert ce.reason == FailoverReason.context_overflow
    assert ce.should_compress


# ── Message-pattern branch ──────────────────────────────────────────


def test_context_overflow_beats_rate_limit_when_both_in_msg() -> None:
    """Some providers return 400 with body 'context length exceeded'.
    Must classify as context_overflow even though no status hint."""
    ce = classify_api_error(_FakeError(
        "Bad request: context length exceeded the limit",
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


def test_overloaded_msg_pattern() -> None:
    ce = classify_api_error(_FakeError("server is busy, try later"))
    assert ce.reason == FailoverReason.overloaded


def test_auth_msg_pattern() -> None:
    ce = classify_api_error(_FakeError("Invalid API key supplied"))
    assert ce.reason == FailoverReason.auth
    assert not ce.retryable


def test_timeout_msg_pattern() -> None:
    ce = classify_api_error(_FakeError("connection timeout"))
    assert ce.reason == FailoverReason.timeout
    assert ce.retryable


def test_timeout_error_class_directly() -> None:
    """Even without a 'timeout' pattern in the msg, a TimeoutError
    instance still classifies."""
    ce = classify_api_error(TimeoutError("bare"))
    assert ce.reason == FailoverReason.timeout


def test_unknown_fallback() -> None:
    ce = classify_api_error(_FakeError("random gibberish"))
    assert ce.reason == FailoverReason.unknown
    assert ce.retryable


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
    """Every retryable reason must have at least one retry; auth
    must have NO retry."""
    rl = backoff_schedule(FailoverReason.rate_limit)
    ov = backoff_schedule(FailoverReason.overloaded)
    to = backoff_schedule(FailoverReason.timeout)
    co = backoff_schedule(FailoverReason.context_overflow)
    auth = backoff_schedule(FailoverReason.auth)
    unk = backoff_schedule(FailoverReason.unknown)

    assert len(rl) >= 2
    assert len(ov) >= 2
    assert len(to) >= 1
    assert len(co) >= 1
    assert len(auth) == 0  # never retry auth
    assert len(unk) >= 1
    # Ascending (each retry waits longer)
    for sched in (rl, ov, to):
        assert sorted(sched) == list(sched)


def test_classified_error_dataclass_defaults() -> None:
    """Defensive: bare ClassifiedError still has sane defaults."""
    ce = ClassifiedError(reason=FailoverReason.unknown)
    assert ce.retryable
    assert not ce.should_compress
    assert ce.status_code is None
