"""ScreenWatcher — periodic screen snapshot + optional OCR.

Two modes:
  * **summary mode** (default): pushes only resolution + display
    count. Cheap, no OCR, no image storage. Useful as a "user is
    actively using the machine" liveness signal.
  * **ocr mode** (opt-in): runs paddleocr/tesseract on the snapshot;
    pushes a percept with extracted text. Heavy — defaults to the
    longest period (60s) and is gated behind explicit config flag
    ``cfg.cognition.perception.screen.ocr_enabled = true``.

Image bytes are NEVER persisted to disk. The screenshot lives in
memory only for the duration of one ``poll_once`` call. The percept
payload carries text (or a summary) but not the raw bitmap.

Privacy posture: identical to ClipboardWatcher — off by default.
Even in summary mode, "user has multiple monitors" is informational
about their setup and could be undesirable; operator opts in.
"""
from __future__ import annotations

import logging
from typing import Any

from xmclaw.cognition.perception.base import PerceptionSource
from xmclaw.cognition.perception_bus import Percept

logger = logging.getLogger(__name__)


class ScreenWatcher(PerceptionSource):
    """Polls screen state. Default summary-only; OCR is opt-in."""

    def __init__(
        self,
        *,
        bus: Any | None = None,
        period_s: float = 30.0,
        ocr_enabled: bool = False,
        ocr_max_chars: int = 2000,
    ) -> None:
        super().__init__(bus=bus, period_s=period_s)
        self._ocr_enabled = bool(ocr_enabled)
        self._ocr_max_chars = max(200, int(ocr_max_chars))
        self._last_text_hash: str | None = None

    @property
    def name(self) -> str:
        return "screen"

    def available(self) -> bool:
        try:
            import mss  # noqa: F401
        except ImportError:
            return False
        except Exception:  # noqa: BLE001
            return False
        return True

    async def poll_once(self) -> list[Percept]:
        try:
            import mss
        except ImportError:
            return []
        try:
            with mss.mss() as sct:
                monitors = list(sct.monitors)
                if not monitors:
                    return []
                # monitors[0] is the "all-monitors" virtual one; the
                # actual displays start at index 1.
                displays = monitors[1:] if len(monitors) > 1 else monitors
                payload: dict[str, Any] = {
                    "display_count": len(displays),
                    "primary_resolution": (
                        f"{displays[0].get('width', 0)}x"
                        f"{displays[0].get('height', 0)}"
                        if displays else "unknown"
                    ),
                }
                if self._ocr_enabled:
                    text = self._try_ocr(sct, displays[0])
                    if text:
                        # Skip pushing when text didn't change —
                        # avoids flooding the bus while the user
                        # stares at a static window.
                        import hashlib
                        h = hashlib.sha1(text.encode(
                            "utf-8", errors="ignore"),
                        ).hexdigest()
                        if h == self._last_text_hash:
                            return []
                        self._last_text_hash = h
                        payload["ocr_text"] = text[: self._ocr_max_chars]
                        payload["ocr_truncated"] = (
                            len(text) > self._ocr_max_chars
                        )
        except Exception as exc:  # noqa: BLE001
            logger.debug("screen_watcher.snap_failed err=%s", exc)
            return []
        return [
            self._make_percept(
                source="screen",
                kind="screen_snapshot" if self._ocr_enabled else "screen_state",
                payload=payload,
                # Summary mode is intentionally low-salience —
                # AttentionFilter shouldn't drag the agent into
                # "user has 2 monitors" reactions.
                suggested_salience=0.55 if self._ocr_enabled else 0.25,
            ),
        ]

    # ── OCR backend ──────────────────────────────────────────────

    def _try_ocr(self, sct: Any, monitor: Any) -> str | None:
        """Best-effort: prefer paddleocr, fallback to pytesseract.
        Returns None if no OCR engine available."""
        # paddleocr branch
        try:
            from paddleocr import PaddleOCR
            from PIL import Image
            shot = sct.grab(monitor)
            img = Image.frombytes("RGB", shot.size, shot.rgb)
            ocr = PaddleOCR(use_angle_cls=False, lang="en")
            result = ocr.ocr(img, cls=False)
            if result and isinstance(result, list):
                texts = [
                    line[1][0]
                    for page in result
                    if page
                    for line in page
                    if line and len(line) >= 2 and line[1]
                ]
                if texts:
                    return "\n".join(texts)
        except ImportError:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.debug("screen_watcher.paddleocr_failed err=%s", exc)
        # pytesseract fallback
        try:
            import pytesseract
            from PIL import Image
            shot = sct.grab(monitor)
            img = Image.frombytes("RGB", shot.size, shot.rgb)
            return pytesseract.image_to_string(img)
        except ImportError:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.debug("screen_watcher.pytesseract_failed err=%s", exc)
        return None


__all__ = ["ScreenWatcher"]
