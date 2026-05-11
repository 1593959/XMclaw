"""History utilities for AgentLoop."""
from typing import Any

# Transient tool errors that earn one automatic retry. Conservative on
# purpose — semantic failures (file not found, bad args) are NOT
# retried because retrying won't help, it'll just delay the LLM
# getting honest feedback. Match against the error STRING since
# tools return ToolResult.error as a free-form message.
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
    "429 ",  # rate-limit; retrying after 0.5s often works for spiky bursts
    "remote disconnected",
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
