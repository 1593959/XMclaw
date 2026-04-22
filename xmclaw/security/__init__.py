"""Security primitives: prompt-injection scanning, secret masking, etc.

This package holds defence-in-depth building blocks that sit between the
trusted runtime and any untrusted input (tool output, web-fetch bodies,
user-owned files the agent loads on the fly). The policies it enforces are
advisory by default — every callsite decides whether to detect_only /
redact / block based on config.
"""
from xmclaw.security.policy import (
    SOURCE_MEMORY_RECALL,
    SOURCE_PROFILE,
    SOURCE_TOOL_RESULT,
    SOURCE_WEB_FETCH,
    PolicyDecision,
    apply_policy,
)
from xmclaw.security.prompt_scanner import (
    Finding,
    PolicyMode,
    ScanResult,
    redact,
    scan_text,
)

__all__ = [
    "Finding",
    "PolicyDecision",
    "PolicyMode",
    "ScanResult",
    "SOURCE_MEMORY_RECALL",
    "SOURCE_PROFILE",
    "SOURCE_TOOL_RESULT",
    "SOURCE_WEB_FETCH",
    "apply_policy",
    "redact",
    "scan_text",
]
