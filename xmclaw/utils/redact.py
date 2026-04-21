"""Secrets scrubber for event payloads.

Every ``BehavioralEvent`` payload passes through ``redact()`` before being
persisted. Conformance test ensures no known secret pattern slips through.

Phase 1: stub with a naive denylist. Phase 2 replaces with a structured
approach (schema-driven redaction per event type).
"""
from __future__ import annotations

import re
from typing import Any

_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "sk-***"),
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"), "sk-ant-***"),
    (re.compile(r"xox[abprs]-[A-Za-z0-9\-]{10,}"), "xox*-***"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "gh*_***"),
    (re.compile(r"AIza[0-9A-Za-z\-_]{35}"), "AIza***"),
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
