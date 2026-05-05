"""B-227 + P0-2: API error classification — full Hermes port.

Replaces the simplified 5-reason classifier with the 12-reason +
7-stage pipeline from ``hermes-agent/agent/error_classifier.py``.

Why upgrade:
  * Pre-P0-2: every 402/billing was misclassified as ``unknown`` and
    retried with backoff (wasteful, account is OUT of money — no
    backoff will help). 4xx format errors got retried too.
  * Pre-P0-2: no fallback signal — XMclaw can't tell its retry loop
    "this is a model_not_found, swap to a different model" because
    the simple classifier didn't have that reason.
  * Pre-P0-2: no provider-specific patterns — Anthropic's "thinking
    block signature invalid" 400 got bucketed as format_error and
    aborted the turn instead of being retried with a fresh request.

Pipeline (priority high → low):
  1. Provider-specific (thinking_signature, long_context_tier)
  2. HTTP status code with message refinement
  3. Error code from response body (resource_exhausted, …)
  4. Message pattern matching (billing vs rate_limit vs context vs auth)
  5. Server disconnect + large session → context_overflow
  6. Transport / timeout heuristics
  7. Fallback: unknown (retryable once with backoff)

Returns ``ClassifiedError`` with four recovery hints:
  * ``retryable`` — outer loop may retry with same args
  * ``should_compress`` — context overflow; run ContextCompressor first
  * ``should_rotate_credential`` — try a different API key (no-op for now)
  * ``should_fallback`` — try a different provider/model

The hints don't all need to be wired today; ``should_rotate_credential``
is set so the future credential-pool work can read it directly. XMclaw
just acts on ``retryable`` + ``should_compress`` for now.

Differences from Hermes:
  * No credential pool integration (XMclaw doesn't have one yet).
  * CN-language patterns retained (上下文长度 / 请稍后再试 / 超过最大长度).
  * Drop-in compatible with the B-227 public API: same enum names for
    the 6 original reasons (rate_limit / overloaded / timeout /
    context_overflow / auth / unknown) so existing call sites still work.
"""
from __future__ import annotations

import enum
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ── Error taxonomy (12 reasons) ──────────────────────────────────────

class FailoverReason(enum.Enum):
    """Why an API call failed — drives recovery action."""

    # Authentication
    auth = "auth"                              # Transient auth (401/403) — refresh/rotate
    auth_permanent = "auth_permanent"          # Auth failed after refresh — abort

    # Billing / quota
    billing = "billing"                        # 402 or confirmed credit exhaustion
    rate_limit = "rate_limit"                  # 429 or throttle — backoff then retry

    # Server-side
    overloaded = "overloaded"                  # 503 / 529 — provider overloaded
    server_error = "server_error"              # 500 / 502 — internal error, retry

    # Transport
    timeout = "timeout"                        # Connection / read timeout

    # Context / payload
    context_overflow = "context_overflow"      # Context too big — compress
    payload_too_large = "payload_too_large"    # 413 — compress payload

    # Model
    model_not_found = "model_not_found"        # 404 / invalid model — fallback

    # Request format
    format_error = "format_error"              # 400 bad request — abort or fallback

    # Provider-specific
    thinking_signature = "thinking_signature"  # Anthropic thinking sig invalid
    long_context_tier = "long_context_tier"    # Anthropic "extra usage" tier gate

    # Catch-all
    unknown = "unknown"                        # Unclassifiable — retry with backoff


# ── Classification result ────────────────────────────────────────────

@dataclass
class ClassifiedError:
    """Structured classification of an API error with recovery hints.

    Hints are independent flags the caller checks instead of re-classifying
    the error itself. None are mutually exclusive — a billing error has
    ``retryable=False`` AND ``should_rotate_credential=True`` AND
    ``should_fallback=True``.
    """

    reason: FailoverReason
    status_code: Optional[int] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    message: str = ""
    error_context: dict = field(default_factory=dict)

    # Recovery action hints
    retryable: bool = True
    should_compress: bool = False
    should_rotate_credential: bool = False
    should_fallback: bool = False
    # Hard-stop signal: agent should abort the turn AND surface the
    # error to the user (e.g. ``auth_permanent`` after credential
    # rotation already failed). Distinct from ``not retryable`` which
    # might still allow a fallback provider.
    should_terminate: bool = False

    @property
    def is_auth(self) -> bool:
        return self.reason in (FailoverReason.auth, FailoverReason.auth_permanent)


# ── Pattern libraries ────────────────────────────────────────────────
# All patterns below are case-insensitive substring matches against
# the lowercased error message string.

_BILLING_PATTERNS = (
    "insufficient credits",
    "insufficient_quota",
    "credit balance",
    "credits have been exhausted",
    "top up your credits",
    "payment required",
    "billing hard limit",
    "exceeded your current quota",
    "account is deactivated",
    "plan does not include",
)

_RATE_LIMIT_PATTERNS = (
    "rate limit", "rate_limit",
    "too many requests", "throttled",
    "requests per minute", "tokens per minute", "requests per day",
    "try again in", "please retry after",
    "resource_exhausted", "rate increased too quickly",
    # AWS Bedrock
    "throttlingexception",
    "too many concurrent requests",
    "servicequotaexceededexception",
)

# "usage limit" / "quota" — could be billing OR rate_limit, needs disambiguation
_USAGE_LIMIT_PATTERNS = (
    "usage limit", "quota", "limit exceeded", "key limit exceeded",
)

_USAGE_LIMIT_TRANSIENT_SIGNALS = (
    "try again", "retry", "resets at", "reset in", "wait",
    "requests remaining", "periodic", "window",
)

_PAYLOAD_TOO_LARGE_PATTERNS = (
    "request entity too large",
    "payload too large",
    "error code: 413",
)

_CONTEXT_OVERFLOW_PATTERNS = (
    "context length", "context size", "maximum context",
    "token limit", "too many tokens", "reduce the length",
    "exceeds the limit", "context window",
    "prompt is too long", "prompt exceeds max length",
    "max_tokens", "maximum number of tokens",
    # vLLM
    "exceeds the max_model_len", "max_model_len",
    "prompt length", "input is too long", "maximum model length",
    # Ollama
    "context length exceeded", "truncating input",
    # llama.cpp
    "slot context", "n_ctx_slot",
    # CN
    "超过最大长度", "上下文长度",
    # Bedrock Converse
    "max input token", "input token",
    "exceeds the maximum number of input tokens",
)

_MODEL_NOT_FOUND_PATTERNS = (
    "is not a valid model",
    "invalid model",
    "model not found",
    "model_not_found",
    "does not exist",
    "no such model",
    "unknown model",
    "unsupported model",
)

_AUTH_PATTERNS = (
    "invalid api key", "invalid_api_key",
    "authentication", "unauthorized", "forbidden",
    "invalid token", "token expired", "token revoked",
    "access denied",
)

# Permanently-broken-account signals: distinguishes "rotate credential
# and try again" from "this account is dead, abort". When matched in
# message body the classifier returns auth_permanent + should_terminate.
_AUTH_PERMANENT_PATTERNS = (
    "account disabled", "account suspended", "account closed",
    "api key revoked", "key has been revoked",
    "organization disabled", "user disabled",
    "billing not active", "no active subscription",
)

_TRANSPORT_ERROR_TYPES = frozenset({
    "ReadTimeout", "ConnectTimeout", "PoolTimeout",
    "ConnectError", "RemoteProtocolError",
    "ConnectionError", "ConnectionResetError",
    "ConnectionAbortedError", "BrokenPipeError",
    "TimeoutError", "ReadError",
    "ServerDisconnectedError",
    "APIConnectionError", "APITimeoutError",
})

_SERVER_DISCONNECT_PATTERNS = (
    "server disconnected",
    "peer closed connection",
    "connection reset by peer",
    "connection was closed",
    "network connection lost",
    "unexpected eof",
    "incomplete chunked read",
)


# ── Public entry point ───────────────────────────────────────────────

def classify_api_error(
    error: Exception,
    *,
    provider: str = "",
    model: str = "",
    approx_tokens: int = 0,
    context_length: int = 200_000,
    num_messages: int = 0,
) -> ClassifiedError:
    """Classify an API exception into a structured recovery recommendation.

    Backward-compatible with the B-227 simplified API: callers that don't
    pass ``approx_tokens / context_length / num_messages`` still get
    correct classification for the 6 simple reasons; the optional args
    only refine generic-400 / disconnect heuristics.

    Args:
        error: The exception from the LLM call.
        provider: Current provider (eg "anthropic", "openrouter").
        model: Current model slug.
        approx_tokens: Approximate tokens of the request payload.
        context_length: Model's context window size.
        num_messages: Length of the message history.
    """
    status_code = _extract_status_code(error)
    error_type = type(error).__name__
    body = _extract_error_body(error)
    error_code = _extract_error_code(body)
    error_msg = _build_error_message(error, body)
    provider_lower = (provider or "").strip().lower()
    model_lower = (model or "").strip().lower()

    def _result(reason: FailoverReason, **overrides) -> ClassifiedError:
        defaults = {
            "reason": reason,
            "status_code": status_code,
            "provider": provider,
            "model": model,
            "message": _extract_message(error, body)[:500],
        }
        defaults.update(overrides)
        return ClassifiedError(**defaults)

    # ── 1. Provider-specific patterns (highest priority) ────────────

    # Anthropic thinking-block signature invalid (400). Don't gate on
    # provider — OpenRouter proxies Anthropic, so the detected provider
    # may be "openrouter" while the error is Anthropic-specific.
    if (status_code == 400 and "signature" in error_msg
            and "thinking" in error_msg):
        return _result(
            FailoverReason.thinking_signature,
            retryable=True, should_compress=False,
        )

    # Anthropic long-context tier gate (429 + "extra usage" + "long context")
    if (status_code == 429 and "extra usage" in error_msg
            and "long context" in error_msg):
        return _result(
            FailoverReason.long_context_tier,
            retryable=True, should_compress=True,
        )

    # ── 2. HTTP status code classification ──────────────────────────
    if status_code is not None:
        classified = _classify_by_status(
            status_code, error_msg, error_code, body,
            provider=provider_lower, model=model_lower,
            approx_tokens=approx_tokens,
            context_length=context_length,
            num_messages=num_messages,
            result_fn=_result,
        )
        if classified is not None:
            return classified

    # ── 3. Error code classification ─────────────────────────────────
    if error_code:
        classified = _classify_by_error_code(error_code, error_msg, _result)
        if classified is not None:
            return classified

    # ── 4. Message pattern matching (no status code) ────────────────
    classified = _classify_by_message(
        error_msg, error_type,
        approx_tokens=approx_tokens,
        context_length=context_length,
        result_fn=_result,
    )
    if classified is not None:
        return classified

    # ── 5. Server disconnect + large session → context_overflow ─────
    is_disconnect = any(p in error_msg for p in _SERVER_DISCONNECT_PATTERNS)
    if is_disconnect and not status_code:
        is_large = (
            approx_tokens > context_length * 0.6
            or approx_tokens > 120_000
            or num_messages > 200
        )
        if is_large:
            return _result(
                FailoverReason.context_overflow,
                retryable=True, should_compress=True,
            )
        return _result(FailoverReason.timeout, retryable=True)

    # ── 6. Transport / timeout heuristics ───────────────────────────
    if (error_type in _TRANSPORT_ERROR_TYPES
            or isinstance(error, (TimeoutError, ConnectionError, OSError))):
        return _result(FailoverReason.timeout, retryable=True)

    # ── 7. Fallback: unknown ────────────────────────────────────────
    return _result(FailoverReason.unknown, retryable=True)


# ── Status code classification ────────────────────────────────────────

def _classify_by_status(
    status_code: int, error_msg: str, error_code: str, body: dict,
    *,
    provider: str, model: str,
    approx_tokens: int, context_length: int, num_messages: int,
    result_fn,
) -> Optional[ClassifiedError]:
    """Classify by HTTP status code with message-aware refinement."""

    if status_code == 401:
        # auth_permanent first — account-disabled / key-revoked won't
        # be fixed by rotating to another credential of the same kind.
        if any(p in error_msg for p in _AUTH_PERMANENT_PATTERNS):
            return result_fn(
                FailoverReason.auth_permanent,
                retryable=False,
                should_rotate_credential=True,
                should_terminate=True,
            )
        return result_fn(
            FailoverReason.auth,
            retryable=False,
            should_rotate_credential=True,
            should_fallback=True,
        )

    if status_code == 403:
        # OpenRouter's 403 "key limit exceeded" is actually billing.
        if "key limit exceeded" in error_msg or "spending limit" in error_msg:
            return result_fn(
                FailoverReason.billing,
                retryable=False,
                should_rotate_credential=True,
                should_fallback=True,
            )
        return result_fn(
            FailoverReason.auth,
            retryable=False, should_fallback=True,
        )

    if status_code == 402:
        return _classify_402(error_msg, result_fn)

    if status_code == 404:
        if any(p in error_msg for p in _MODEL_NOT_FOUND_PATTERNS):
            return result_fn(
                FailoverReason.model_not_found,
                retryable=False, should_fallback=True,
            )
        # Generic 404 — could be wrong endpoint path, proxy glitch.
        # Don't claim model-not-found without the signal; surface as unknown.
        return result_fn(FailoverReason.unknown, retryable=True)

    if status_code == 413:
        return result_fn(
            FailoverReason.payload_too_large,
            retryable=True, should_compress=True,
        )

    if status_code == 429:
        # long_context_tier is checked above; this is a normal rate limit.
        return result_fn(
            FailoverReason.rate_limit,
            retryable=True,
            should_rotate_credential=True,
            should_fallback=True,
        )

    if status_code == 400:
        return _classify_400(
            error_msg, error_code, body,
            provider=provider, model=model,
            approx_tokens=approx_tokens,
            context_length=context_length,
            num_messages=num_messages,
            result_fn=result_fn,
        )

    if status_code in (500, 502):
        return result_fn(FailoverReason.server_error, retryable=True)

    if status_code in (503, 529):
        return result_fn(FailoverReason.overloaded, retryable=True)

    # Other 4xx — non-retryable
    if 400 <= status_code < 500:
        return result_fn(
            FailoverReason.format_error,
            retryable=False, should_fallback=True,
        )

    # Other 5xx — retryable
    if 500 <= status_code < 600:
        return result_fn(FailoverReason.server_error, retryable=True)

    return None


def _classify_402(error_msg: str, result_fn) -> ClassifiedError:
    """Disambiguate 402: billing exhaustion vs transient usage limit.

    Some providers return "Usage limit, try again in 5 minutes" as a 402
    when it's actually a periodic quota that resets. Detect transient
    signals to avoid burning the credential pool on a recoverable error.
    """
    has_usage_limit = any(p in error_msg for p in _USAGE_LIMIT_PATTERNS)
    has_transient_signal = any(p in error_msg for p in _USAGE_LIMIT_TRANSIENT_SIGNALS)

    if has_usage_limit and has_transient_signal:
        return result_fn(
            FailoverReason.rate_limit,
            retryable=True,
            should_rotate_credential=True,
            should_fallback=True,
        )

    return result_fn(
        FailoverReason.billing,
        retryable=False,
        should_rotate_credential=True,
        should_fallback=True,
    )


def _classify_400(
    error_msg: str, error_code: str, body: dict,
    *,
    provider: str, model: str,
    approx_tokens: int, context_length: int, num_messages: int,
    result_fn,
) -> ClassifiedError:
    """Classify 400 Bad Request — context_overflow / format_error / generic."""
    if any(p in error_msg for p in _CONTEXT_OVERFLOW_PATTERNS):
        return result_fn(
            FailoverReason.context_overflow,
            retryable=True, should_compress=True,
        )

    if any(p in error_msg for p in _MODEL_NOT_FOUND_PATTERNS):
        return result_fn(
            FailoverReason.model_not_found,
            retryable=False, should_fallback=True,
        )

    # Some providers return rate_limit / billing as 400 (not 429 / 402).
    if any(p in error_msg for p in _RATE_LIMIT_PATTERNS):
        return result_fn(
            FailoverReason.rate_limit,
            retryable=True,
            should_rotate_credential=True,
            should_fallback=True,
        )
    if any(p in error_msg for p in _BILLING_PATTERNS):
        return result_fn(
            FailoverReason.billing,
            retryable=False,
            should_rotate_credential=True,
            should_fallback=True,
        )

    # Generic 400 + large session → probable context overflow.
    # Anthropic sometimes returns just "Error" when context is too large.
    err_body_msg = ""
    if isinstance(body, dict):
        err_obj = body.get("error", {})
        if isinstance(err_obj, dict):
            err_body_msg = str(err_obj.get("message") or "").strip().lower()
        if not err_body_msg:
            err_body_msg = str(body.get("message") or "").strip().lower()

    is_generic = len(err_body_msg) < 30 or err_body_msg in ("error", "")
    is_large = (
        approx_tokens > context_length * 0.4
        or approx_tokens > 80_000
        or num_messages > 80
    )
    if is_generic and is_large:
        return result_fn(
            FailoverReason.context_overflow,
            retryable=True, should_compress=True,
        )

    return result_fn(
        FailoverReason.format_error,
        retryable=False, should_fallback=True,
    )


# ── Error code classification ────────────────────────────────────────

def _classify_by_error_code(
    error_code: str, error_msg: str, result_fn,
) -> Optional[ClassifiedError]:
    """Classify by structured error codes from the response body."""
    code = error_code.lower()

    if code in ("resource_exhausted", "throttled", "rate_limit_exceeded"):
        return result_fn(
            FailoverReason.rate_limit,
            retryable=True, should_rotate_credential=True,
        )

    if code in ("insufficient_quota", "billing_not_active", "payment_required"):
        return result_fn(
            FailoverReason.billing,
            retryable=False,
            should_rotate_credential=True, should_fallback=True,
        )

    if code in ("model_not_found", "model_not_available", "invalid_model"):
        return result_fn(
            FailoverReason.model_not_found,
            retryable=False, should_fallback=True,
        )

    if code in ("context_length_exceeded", "max_tokens_exceeded"):
        return result_fn(
            FailoverReason.context_overflow,
            retryable=True, should_compress=True,
        )

    return None


# ── Message pattern classification ────────────────────────────────────

def _classify_by_message(
    error_msg: str, error_type: str,
    *,
    approx_tokens: int, context_length: int,
    result_fn,
) -> Optional[ClassifiedError]:
    """Classify by message patterns when no HTTP status code is available."""

    if any(p in error_msg for p in _PAYLOAD_TOO_LARGE_PATTERNS):
        return result_fn(
            FailoverReason.payload_too_large,
            retryable=True, should_compress=True,
        )

    # Usage-limit needs disambiguation (transient signal vs billing).
    has_usage_limit = any(p in error_msg for p in _USAGE_LIMIT_PATTERNS)
    if has_usage_limit:
        has_transient = any(p in error_msg for p in _USAGE_LIMIT_TRANSIENT_SIGNALS)
        if has_transient:
            return result_fn(
                FailoverReason.rate_limit,
                retryable=True,
                should_rotate_credential=True, should_fallback=True,
            )
        return result_fn(
            FailoverReason.billing,
            retryable=False,
            should_rotate_credential=True, should_fallback=True,
        )

    if any(p in error_msg for p in _BILLING_PATTERNS):
        return result_fn(
            FailoverReason.billing,
            retryable=False,
            should_rotate_credential=True, should_fallback=True,
        )

    if any(p in error_msg for p in _RATE_LIMIT_PATTERNS):
        return result_fn(
            FailoverReason.rate_limit,
            retryable=True,
            should_rotate_credential=True, should_fallback=True,
        )

    # Context overflow first (some providers say "context length exceeded"
    # without a status code) — must beat rate_limit because both can
    # mention "limit".
    if any(p in error_msg for p in _CONTEXT_OVERFLOW_PATTERNS):
        return result_fn(
            FailoverReason.context_overflow,
            retryable=True, should_compress=True,
        )

    if any(p in error_msg for p in _AUTH_PERMANENT_PATTERNS):
        return result_fn(
            FailoverReason.auth_permanent,
            retryable=False,
            should_rotate_credential=True, should_terminate=True,
        )

    if any(p in error_msg for p in _AUTH_PATTERNS):
        return result_fn(
            FailoverReason.auth,
            retryable=False,
            should_rotate_credential=True, should_fallback=True,
        )

    if any(p in error_msg for p in _MODEL_NOT_FOUND_PATTERNS):
        return result_fn(
            FailoverReason.model_not_found,
            retryable=False, should_fallback=True,
        )

    return None


# ── Helpers ──────────────────────────────────────────────────────────

def _extract_status_code(error: Exception) -> Optional[int]:
    """Walk the error and its cause chain to find an HTTP status code."""
    current: Any = error
    for _ in range(5):
        code = getattr(current, "status_code", None)
        if isinstance(code, int):
            return code
        code = getattr(current, "status", None)
        if isinstance(code, int) and 100 <= code < 600:
            return code
        cause = (
            getattr(current, "__cause__", None)
            or getattr(current, "__context__", None)
        )
        if cause is None or cause is current:
            break
        current = cause
    return None


def _extract_error_body(error: Exception) -> dict:
    """Extract the structured error body from an SDK exception."""
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        return body
    response = getattr(error, "response", None)
    if response is not None:
        try:
            json_body = response.json()
            if isinstance(json_body, dict):
                return json_body
        except Exception:  # noqa: BLE001
            pass
    return {}


def _extract_error_code(body: dict) -> str:
    """Extract a structured error code string from the response body."""
    if not body:
        return ""
    error_obj = body.get("error", {})
    if isinstance(error_obj, dict):
        code = error_obj.get("code") or error_obj.get("type") or ""
        if isinstance(code, str) and code.strip():
            return code.strip()
    code = body.get("code") or body.get("error_code") or ""
    if isinstance(code, (str, int)):
        return str(code).strip()
    return ""


def _extract_message(error: Exception, body: dict) -> str:
    """Pick the most informative error message available."""
    if body:
        error_obj = body.get("error", {})
        if isinstance(error_obj, dict):
            msg = error_obj.get("message", "")
            if isinstance(msg, str) and msg.strip():
                return msg.strip()
        msg = body.get("message", "")
        if isinstance(msg, str) and msg.strip():
            return msg.strip()
    return str(error)


def _build_error_message(error: Exception, body: dict) -> str:
    """Build a comprehensive lowercased message string for pattern matching.

    str(error) alone often misses the body (OpenAI SDK's APIStatusError
    only shows the first arg). Append body.error.message + parse
    OpenRouter's wrapped metadata.raw inner errors so patterns like
    "context length exceeded" match even when only present in the
    wrapped JSON.
    """
    parts = [str(error).lower()]
    if isinstance(body, dict):
        err_obj = body.get("error", {})
        if isinstance(err_obj, dict):
            body_msg = str(err_obj.get("message") or "").lower()
            if body_msg and body_msg not in parts[0]:
                parts.append(body_msg)
            # OpenRouter wraps upstream errors: error.metadata.raw is JSON.
            metadata = err_obj.get("metadata", {})
            if isinstance(metadata, dict):
                raw_json = metadata.get("raw") or ""
                if isinstance(raw_json, str) and raw_json.strip():
                    try:
                        inner = json.loads(raw_json)
                        if isinstance(inner, dict):
                            inner_err = inner.get("error", {})
                            if isinstance(inner_err, dict):
                                inner_msg = str(inner_err.get("message") or "").lower()
                                if inner_msg and inner_msg not in " ".join(parts):
                                    parts.append(inner_msg)
                    except (json.JSONDecodeError, TypeError):
                        pass
        flat_msg = str(body.get("message") or "").lower()
        if flat_msg and flat_msg not in " ".join(parts):
            parts.append(flat_msg)
    return " ".join(parts)


# ── Backoff schedules per reason ─────────────────────────────────────

_BACKOFF_MS: dict[FailoverReason, tuple[int, ...]] = {
    FailoverReason.rate_limit:         (1500, 4500, 9000),   # 1.5s / 4.5s / 9s
    FailoverReason.overloaded:         (2000, 5000, 10000),  # 2s / 5s / 10s
    FailoverReason.server_error:       (1500, 4500),         # 1.5s / 4.5s
    FailoverReason.timeout:            (1000, 3000),         # 1s / 3s
    FailoverReason.context_overflow:   (500,),               # one retry, fast
    FailoverReason.payload_too_large:  (500,),               # one retry after compress
    FailoverReason.long_context_tier:  (1000,),              # one retry after compress
    FailoverReason.thinking_signature: (500,),               # one retry, fresh request
    FailoverReason.auth:               (),                   # never retry — rotate
    FailoverReason.auth_permanent:     (),                   # never retry
    FailoverReason.billing:            (),                   # never retry — rotate / fallback
    FailoverReason.model_not_found:    (),                   # never retry — fallback
    FailoverReason.format_error:       (),                   # never retry — abort
    FailoverReason.unknown:            (1000, 3000),         # 1s / 3s, conservative
}


def backoff_schedule(reason: FailoverReason) -> tuple[int, ...]:
    """Per-reason backoff schedule in milliseconds.

    Returned tuple length = max retries; values = sleep before each retry.
    Empty tuple = don't retry (auth / billing / model_not_found / format_error).
    """
    return _BACKOFF_MS.get(reason, ())


__all__ = [
    "FailoverReason",
    "ClassifiedError",
    "classify_api_error",
    "backoff_schedule",
]
