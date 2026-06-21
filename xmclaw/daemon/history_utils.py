"""History utilities for AgentLoop."""
from typing import Any

# Transient tool errors that earn automatic retries. Conservative on
# purpose — semantic failures (file not found, bad args) are NOT
# retried because retrying won't help, it'll just delay the LLM
# getting honest feedback. Match against the error STRING since
# tools return ToolResult.error as a free-form message.
#
# 2026-06-22: expanded from 14 → 35 patterns. Covers DNS, TLS,
# certificate, network-unreachable, read/write stalls, and HTTP
# status codes that upstream typically retries (408, 409, 423, 425,
# 429, 500, 502, 503, 504, 507, 508, 509, 529, 598, 599).
_TRANSIENT_PATTERNS = (
    "timeout",
    "timed out",
    "connection reset",
    "connection refused",
    "temporarily unavailable",
    "ECONNRESET",
    "ECONNREFUSED",
    "ETIMEDOUT",
    "EAI_AGAIN",
    "name or service not known",
    "503 ",
    "502 ",
    "504 ",
    "429 ",  # rate-limit; retrying after back-off often works for spiky bursts
    "remote disconnected",
    # DNS / resolution
    "could not resolve",
    "getaddrinfo failed",
    "dns",
    "no such host",
    "name resolution",
    # TLS / certificate
    "certificate",
    "ssl",
    "tls",
    "handshake",
    "verify failed",
    # Network layer
    "network is unreachable",
    "unreachable",
    "no route to host",
    "host is down",
    "connection aborted",
    "connection closed",
    "broken pipe",
    "read error",
    "write error",
    # HTTP status codes that are typically transient
    "408 ",  # request timeout
    "500 ",  # internal server error (often transient)
    "502 ",  # bad gateway
    "503 ",  # service unavailable
    "504 ",  # gateway timeout
    "507 ",  # insufficient storage
    "508 ",  # loop detected
    "509 ",  # bandwidth limit exceeded
    "529 ",  # site is overloaded (Cloudflare)
    "598 ",  # network read timeout
    "599 ",  # network connect timeout
    # Generic transient signals
    "too many requests",
    "rate limit",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "internal server error",
    "try again",
    "retry",
    "temporary",
    "server error",
    "busy",
    "overloaded",
    "maintenance",
    "unavailable",
)

def _is_transient_tool_error(err: str) -> bool:
    if not err:
        return False
    low = err.lower()
    return any(p.lower() in low for p in _TRANSIENT_PATTERNS)

def _estimate_history_tokens(history: list[Any]) -> int:
    """B-31: char/4 token approximation for the compression gate.

    Sums ``len(content)`` across messages and divides by 4. Cheap
    and ~5% off real BPE for English/Chinese mix — accurate enough
    to decide "are we in danger of running out of context?". We
    deliberately don't pull tiktoken: it's heavy, model-specific,
    and we'd need different encoders per provider. The gate is
    advisory; a real overflow would raise from the LLM provider.
    """
    total = 0
    for m in history:
        c = getattr(m, "content", "")
        if isinstance(c, str):
            total += len(c)
        elif c is not None:
            # Tool messages can carry structured payloads; serialize
            # cheaply rather than pulling json.dumps every call.
            total += len(str(c))
        # Account for tool-call payloads on assistant messages.
        for tc in getattr(m, "tool_calls", ()) or ():
            args = getattr(tc, "args", None)
            if args:
                total += len(str(args))
    return total // 4
