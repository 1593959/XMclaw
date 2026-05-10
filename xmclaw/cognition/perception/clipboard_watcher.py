"""ClipboardWatcher — pushes a percept when the clipboard content
changes.

Uses ``pyperclip`` cross-platform. If it can't import → watcher
silent.

Privacy posture: clipboard contents are sensitive (passwords,
private text). The watcher is **off by default**; operators must
opt in via ``cfg.cognition.perception.clipboard.enabled = true``.
The percept payload includes a length-capped preview (default 500
chars) — never the full content unless explicitly asked.

Tagging:
  * URL detection → kind="url_copied", salience 0.6
  * Code-block detection (3+ lines, presence of indentation /
    keywords) → kind="code_copied", salience 0.55
  * Otherwise → kind="text_copied", salience 0.4
"""
from __future__ import annotations

import logging
import re
from typing import Any

from xmclaw.cognition.perception.base import PerceptionSource
from xmclaw.cognition.perception_bus import Percept

logger = logging.getLogger(__name__)


_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)


def _classify(text: str) -> tuple[str, float]:
    """Return (kind, suggested_salience) based on cheap heuristics."""
    if _URL_RE.search(text):
        return "url_copied", 0.6
    # Code-block guess: 3+ lines, presence of typical code tokens.
    lines = text.splitlines()
    if len(lines) >= 3 and any(
        kw in text for kw in (
            "def ", "function ", "class ", "import ",
            "    ", "\t", "{", "}", "=>",
        )
    ):
        return "code_copied", 0.55
    return "text_copied", 0.4


class ClipboardWatcher(PerceptionSource):
    """Polls clipboard at ``period_s`` (default 3 s) and pushes when
    content changes. Caps payload preview at ``preview_chars``."""

    def __init__(
        self,
        *,
        bus: Any | None = None,
        period_s: float = 3.0,
        preview_chars: int = 500,
    ) -> None:
        super().__init__(bus=bus, period_s=period_s)
        self._preview_chars = max(50, int(preview_chars))
        self._last_content: str | None = None

    @property
    def name(self) -> str:
        return "clipboard"

    def available(self) -> bool:
        try:
            import pyperclip  # noqa: F401
        except ImportError:
            return False
        except Exception:  # noqa: BLE001
            return False
        return True

    async def poll_once(self) -> list[Percept]:
        try:
            import pyperclip
        except ImportError:
            return []
        try:
            content = pyperclip.paste()
        except Exception as exc:  # noqa: BLE001
            logger.debug("clipboard_watcher.paste_failed err=%s", exc)
            return []
        if not isinstance(content, str):
            return []
        content = content.strip()
        if not content or content == self._last_content:
            return []
        self._last_content = content
        kind, salience = _classify(content)
        preview = content[: self._preview_chars]
        truncated = len(content) > self._preview_chars
        return [
            self._make_percept(
                source="clipboard",
                kind=kind,
                payload={
                    "preview": preview,
                    "char_count": len(content),
                    "truncated": truncated,
                },
                suggested_salience=salience,
            ),
        ]


__all__ = ["ClipboardWatcher"]
