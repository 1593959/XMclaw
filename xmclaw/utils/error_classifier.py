"""B-227: API error classification — simplified port of Hermes
``agent/error_classifier.py`` (834 LOC) trimmed to the 5 categories
that XMclaw actually has business cases for.

Pre-B-227 every LLM exception was caught as a bare ``Exception`` in
``agent_loop.run_turn`` and the turn died immediately. Real-data:
~10% of turns failed on transient rate_limit / overloaded that would
have succeeded on retry. This module gives the agent loop a cheap
way to decide:

  * retry now (rate_limit / overloaded / timeout)
  * compress context then retry (context_overflow)
  * abort with explanation (auth / unknown)

We DON'T port the full 834-LOC pipeline. The simplifications:
  * 5 reasons instead of Hermes's 12
  * No credential pool rotation (XMclaw doesn't have one yet)
  * No provider-specific quirks (thinking-sig / tier-gate) — those
    only matter for Anthropic direct, our users mostly hit shims
  * Pattern lib is the proven Hermes one (CN + EN supported)

Returns ``ClassifiedError`` with structured hints; the retry loop
in ``agent_loop`` consults ``.retryable / .should_compress`` instead
of re-classifying the same string twice.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass


class FailoverReason(enum.Enum):
    """Why an LLM call failed — drives recovery action."""

    rate_limit = "rate_limit"          # 429 / quota throttle — retry with backoff
    overloaded = "overloaded"          # 503 / 529 — server overloaded, retry
    timeout = "timeout"                # connection / read timeout — retry once
    context_overflow = "context_overflow"  # token limit — compress then retry
    auth = "auth"                      # 401 / 403 — abort, can't retry
    unknown = "unknown"                # everything else — retry once with backoff


@dataclass
class ClassifiedError:
    """Structured classification + recovery hints."""

    reason: FailoverReason
    status_code: int | None = None
    message: str = ""
    retryable: bool = True
    should_compress: bool = False


# Hermes-tested pattern libs (CN + EN). Adapted from
# hermes-agent/agent/error_classifier.py:88-183.

_RATE_LIMIT_PATTERNS = (
    "rate limit", "rate_limit", "too many requests", "throttled",
    "requests per minute", "tokens per minute", "requests per day",
    "try again in", "please retry after", "resource_exhausted",
    "rate increased too quickly",
    "throttlingexception", "too many concurrent requests",
    "servicequotaexceededexception",
)

_OVERLOADED_PATTERNS = (
    "overloaded", "service unavailable", "temporarily unavailable",
    "server is busy", "请稍后再试",
)

_CONTEXT_OVERFLOW_PATTERNS = (
    "context length", "context size", "maximum context",
    "token limit", "too many tokens", "reduce the length",
    "exceeds the limit", "context window", "prompt is too long",
    "prompt exceeds max length", "maximum number of tokens",
    "exceeds the max_model_len", "max_model_len",
    "input is too long", "maximum model length",
    "context length exceeded", "truncating input",
    # llama.cpp
    "slot context", "n_ctx_slot",
    # CN
    "超过最大长度", "上下文长度",
    # Bedrock Converse
    "max input token", "exceeds the maximum number of input tokens",
)

_AUTH_PATTERNS = (
    "invalid api key", "incorrect api key", "authentication failed",
    "unauthorized", "forbidden", "api key not found",
    "api_key", "invalid_api_key",
)

_TIMEOUT_PATTERNS = (
    "timeout", "timed out", "read timeout", "connection timeout",
    "ssl handshake timeout", "asyncio.timeouterror",
)


def _extract_status_code(error: Exception) -> int | None:
    """Pull HTTP status from common SDK exception shapes.

    httpx.HTTPStatusError / openai.APIStatusError / anthropic.APIStatusError
    all expose ``.status_code`` directly. Some shims attach it on
    ``.response.status_code``.
    """
    for path in ("status_code", "code", "http_status"):
        v = getattr(error, path, None)
        if isinstance(v, int):
            return v
    resp = getattr(error, "response", None)
    if resp is not None:
        v = getattr(resp, "status_code", None)
        if isinstance(v, int):
            return v
    return None


def _extract_error_message(error: Exception) -> str:
    """Best-effort error message extraction (lowercased for matching).

    Some SDK exceptions only show the first arg in ``str(error)`` and
    bury the real message in a body attribute. We concatenate both
    so pattern matching is more reliable.
    """
    parts = [str(error).lower()]
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        err_obj = body.get("error", body)
        if isinstance(err_obj, dict):
            msg = err_obj.get("message")
            if isinstance(msg, str):
                parts.append(msg.lower())
    return " ".join(parts)


def _matches_any(message: str, patterns: tuple[str, ...]) -> bool:
    """Lower-case substring matching against pattern tuple."""
    return any(p in message for p in patterns)


def classify_api_error(
    error: Exception,
    *,
    provider: str = "",
    model: str = "",
) -> ClassifiedError:
    """Classify an LLM call exception into a recovery recommendation.

    Pipeline (priority high → low, matches Hermes order):
      1. HTTP status code (cheap, definitive)
      2. Message pattern: context_overflow (avoid mis-classifying
         as rate_limit just because both mention "limit")
      3. Message pattern: rate_limit / overloaded / auth / timeout
      4. Fallback: unknown (retryable once with backoff)
    """
    status = _extract_status_code(error)
    msg = _extract_error_message(error)

    # 1. Status code (when present, take its hint first)
    if status is not None:
        if status == 429:
            return ClassifiedError(
                reason=FailoverReason.rate_limit, status_code=status,
                message=msg[:500], retryable=True,
            )
        if status in (503, 529):
            return ClassifiedError(
                reason=FailoverReason.overloaded, status_code=status,
                message=msg[:500], retryable=True,
            )
        if status in (401, 403):
            return ClassifiedError(
                reason=FailoverReason.auth, status_code=status,
                message=msg[:500], retryable=False,
            )
        if status == 413:
            return ClassifiedError(
                reason=FailoverReason.context_overflow, status_code=status,
                message=msg[:500], retryable=True, should_compress=True,
            )
        # 400 might be context_overflow in disguise — fall through
        # to message matching below.

    # 2. Context overflow first (some providers return 400 + body
    # message "context_length_exceeded" — must beat rate_limit match)
    if _matches_any(msg, _CONTEXT_OVERFLOW_PATTERNS):
        return ClassifiedError(
            reason=FailoverReason.context_overflow, status_code=status,
            message=msg[:500], retryable=True, should_compress=True,
        )

    # 3. Other message patterns
    if _matches_any(msg, _RATE_LIMIT_PATTERNS):
        return ClassifiedError(
            reason=FailoverReason.rate_limit, status_code=status,
            message=msg[:500], retryable=True,
        )
    if _matches_any(msg, _OVERLOADED_PATTERNS):
        return ClassifiedError(
            reason=FailoverReason.overloaded, status_code=status,
            message=msg[:500], retryable=True,
        )
    if _matches_any(msg, _AUTH_PATTERNS):
        return ClassifiedError(
            reason=FailoverReason.auth, status_code=status,
            message=msg[:500], retryable=False,
        )
    if _matches_any(msg, _TIMEOUT_PATTERNS) or isinstance(
        error, (TimeoutError,),
    ):
        return ClassifiedError(
            reason=FailoverReason.timeout, status_code=status,
            message=msg[:500], retryable=True,
        )

    # 4. Fallback
    return ClassifiedError(
        reason=FailoverReason.unknown, status_code=status,
        message=msg[:500], retryable=True,
    )


# Recovery scheduler: how to back off per reason.
_BACKOFF_MS: dict[FailoverReason, tuple[int, ...]] = {
    FailoverReason.rate_limit:        (1500, 4500, 9000),   # 1.5s / 4.5s / 9s
    FailoverReason.overloaded:        (2000, 5000, 10000),  # 2s / 5s / 10s
    FailoverReason.timeout:           (1000, 3000),         # 1s / 3s
    FailoverReason.context_overflow:  (500,),               # one retry, fast
    FailoverReason.auth:              (),                   # never retry
    FailoverReason.unknown:           (1000, 3000),         # 1s / 3s, conservative
}


def backoff_schedule(reason: FailoverReason) -> tuple[int, ...]:
    """Per-reason backoff schedule in milliseconds.

    Returned tuple length = max retries; values = sleep before each
    retry. Empty tuple = don't retry (auth).
    """
    return _BACKOFF_MS.get(reason, ())


__all__ = [
    "FailoverReason",
    "ClassifiedError",
    "classify_api_error",
    "backoff_schedule",
]
