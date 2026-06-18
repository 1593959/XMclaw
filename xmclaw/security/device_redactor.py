"""Device-level PII redactor for mobile observation results.

对 ``obs.tree`` 和 ``obs.screenshot`` 返回的文本内容进行脱敏，
用 ``[REDACTED]`` 替换检测到的敏感信息（手机号、身份证号、银行卡号）。

Usage::

    from xmclaw.security.device_redactor import DeviceRedactor
    redactor = DeviceRedactor()
    safe_nodes = redactor.redact_tree(raw_nodes)
"""
from __future__ import annotations

import re
from typing import Any

# ------------------------------------------------------------------
# PII regex patterns
# ------------------------------------------------------------------
_PII_PATTERNS: dict[str, re.Pattern[str]] = {
    "phone": re.compile(r"1[3-9]\d{9}"),
    "id_card": re.compile(r"\d{17}[\dXx]|\d{15}"),
    "bank_card": re.compile(r"\d{16,19}"),
}

_REDACTED = "[REDACTED]"


class DeviceRedactor:
    """Redact personally identifiable information from device observation data."""

    def redact_text(self, text: str) -> str:
        """Replace PII substrings in *text* with ``[REDACTED]``.

        Args:
            text: Raw text potentially containing sensitive numbers.

        Returns:
            Sanitised text with phone numbers, ID cards and bank cards masked.
        """
        if not isinstance(text, str):
            return text
        for _name, pat in _PII_PATTERNS.items():
            text = pat.sub(_REDACTED, text)
        return text

    def redact_tree(self, nodes: list[dict]) -> list[dict]:
        """Return a deep-copied list of nodes with ``text`` and ``desc`` redacted.

        Each node is expected to follow the ``obs.tree`` Node DTO schema
        (see ``docs/android_protocol_v1.md`` §6).  Only the fields that
        actually contain human-readable text are touched; structural data
        (``bounds``, ``center``, ``res_id``, etc.) is left intact.

        Args:
            nodes: Flat list of node dictionaries from the companion app.

        Returns:
            A new list of node dicts; the original *nodes* is not modified.
        """
        result: list[dict] = []
        for node in nodes:
            if not isinstance(node, dict):
                result.append(node)
                continue
            new_node: dict[str, Any] = dict(node)
            for key in ("text", "desc"):
                val = new_node.get(key)
                if isinstance(val, str):
                    new_node[key] = self.redact_text(val)
            result.append(new_node)
        return result
