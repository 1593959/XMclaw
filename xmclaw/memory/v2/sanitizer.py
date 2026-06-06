"""Pre-write input sanitization for memory poisoning defense.

Wave-3 fix (2026-06-06): blocks known Sleeper Memory Poisoning
patterns before they reach the store. High-trust provenances
(user_confirmed, manual_ui, persona_file) bypass the heuristic.
"""
from __future__ import annotations

import re
from typing import Any


_SUSPICIOUS_PATTERNS = [
    # Explicit command injection attempts
    r"ignore\s+(previous|above|all)\s+instructions",
    r"system\s*prompt\s*:\s*",
    r"you\s+are\s+now\s+",
    r"new\s+role\s*:\s*",
    # Sleeper-style delayed activation
    r"when\s+user\s+says\s+.*\s+then\s+",
    r"if\s+.*\s+activate\s+",
    # Policy override attempts
    r"override\s+(safety|security|policy)",
    r"disable\s+(filter|guard|check)",
]


class MemorySanitizer:
    """Lightweight pre-write sanitizer for memory poisoning defense."""

    def __init__(self) -> None:
        self._patterns = [re.compile(p, re.IGNORECASE) for p in _SUSPICIOUS_PATTERNS]

    def check(self, text: str, provenance: str) -> tuple[bool, str]:
        """Returns (is_safe, reason). Blocks if suspicious.

        High-trust provenances bypass heuristic — we assume user-curated
        or manually-entered facts are intentional.
        """
        if provenance in ("user_confirmed", "manual_ui", "persona_file"):
            return True, "high_trust"
        for p in self._patterns:
            if p.search(text):
                return False, f"suspicious_pattern:{p.pattern[:30]}"
        return True, "clean"


# Singleton for lazy import
_sanitizer_instance: MemorySanitizer | None = None


def get_sanitizer() -> MemorySanitizer:
    global _sanitizer_instance
    if _sanitizer_instance is None:
        _sanitizer_instance = MemorySanitizer()
    return _sanitizer_instance
