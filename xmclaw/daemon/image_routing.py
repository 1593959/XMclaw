"""Intake-time image routing for chat attachments.

Design lifted from Hermes-agent (`agent/image_routing.py` +
`tui_gateway/server.py::_enrich_with_attached_images`, see
https://github.com/NousResearch/hermes-agent). The principle:

  **Resolve the image-vs-text-only mismatch ONCE at intake**
  (the moment the WS user-frame lands), persist the result into
  the user message, and let history-replay / translators / hop
  loops stay completely unaware of "is this model vision-capable".

The alternative — having the LLM translator strip / OCR per-hop —
produces the OpenClaw #29290 failure mode (images in failed turns
poison the session, can't be pruned because no following assistant
reply) AND the "every hop re-OCRs every history image" perf
disaster we hit on 2026-05-28.

Modes
-----
``native`` — keep the raw image attachments; just prepend a
  `[Image attached at: <path>]` hint to the user text so the model
  has a reference if it wants to call a tool against the file
  later. Used when the active LLM profile is vision-capable.

``text`` — drop image attachments from the LLM payload entirely.
  OCR each image locally (rapidocr → paddleocr → pytesseract) and
  prepend `[image OCR | basename]\\n<extracted text>` to the user
  text. The user-visible UI still shows the thumbnail (WS already
  echoed the attachments); only the LLM sees text.

OCR is local-first by deliberate design — the user has rapidocr
installed and screen_ocr already exercises it, so we get sub-second
warm-path latency without any cloud round-trip. If we ever want a
cloud "describe this image" fallback for richer scene description,
``_describe_via_aux_vision`` is the seam — currently a no-op stub.

The OCR engine is a **process singleton**: cold-init is ~10s
(ONNX warmup), warm calls are ~1s/image. We cache the instance
in this module so a long chat session doesn't pay the cold-init
on every attached image.
"""
from __future__ import annotations

import os
from pathlib import Path
from threading import Lock
from typing import Any, Sequence

from xmclaw.utils.log import get_logger

log = get_logger(__name__)


# ─── Vision-capability lookup (pure config, no network) ────────────

def supports_vision(config: dict[str, Any] | None, profile_id: str | None) -> bool:
    """Does the active LLM profile support image inputs natively?

    Reads ``llm.profiles[*].supports_vision`` from config. Falls
    back to the top-level ``llm.anthropic.supports_vision`` if
    ``profile_id`` is None or doesn't match a profile entry.

    Default when unset: ``True`` (most modern frontier models do).
    Explicitly set ``supports_vision: false`` on profiles backed
    by text-only models (DeepSeek V3/V4, smaller open-source).
    """
    if not isinstance(config, dict):
        return True
    llm_cfg = config.get("llm") or {}
    if not isinstance(llm_cfg, dict):
        return True

    if isinstance(profile_id, str) and profile_id:
        for prof in (llm_cfg.get("profiles") or []):
            if not isinstance(prof, dict):
                continue
            if prof.get("id") == profile_id:
                val = prof.get("supports_vision")
                if isinstance(val, bool):
                    return val
                break  # profile found but field unset → fall through

    # Legacy top-level anthropic block.
    top = llm_cfg.get("anthropic") or {}
    if isinstance(top, dict):
        val = top.get("supports_vision")
        if isinstance(val, bool):
            return val

    return True


# ─── Local OCR (rapidocr singleton, paddleocr / tesseract fallback) ──

_RAPIDOCR_ENGINE: Any = None
_RAPIDOCR_LOCK = Lock()
_RAPIDOCR_FAILED_IMPORT = False  # remember ImportError so we don't retry


def _get_rapidocr() -> Any | None:
    """Return the process-singleton RapidOCR engine, or None if the
    package isn't installed. Cold init is ~10s — we pay it once
    per daemon lifetime."""
    global _RAPIDOCR_ENGINE, _RAPIDOCR_FAILED_IMPORT
    if _RAPIDOCR_ENGINE is not None:
        return _RAPIDOCR_ENGINE
    if _RAPIDOCR_FAILED_IMPORT:
        return None
    with _RAPIDOCR_LOCK:
        if _RAPIDOCR_ENGINE is not None:
            return _RAPIDOCR_ENGINE
        if _RAPIDOCR_FAILED_IMPORT:
            return None
        try:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore
            _RAPIDOCR_ENGINE = RapidOCR()
            log.info("image_routing: rapidocr engine initialised (singleton)")
            return _RAPIDOCR_ENGINE
        except ImportError:
            _RAPIDOCR_FAILED_IMPORT = True
            log.debug("image_routing: rapidocr_onnxruntime not installed")
            return None
        except Exception as exc:  # noqa: BLE001
            log.warning("image_routing: rapidocr init failed: %s", exc)
            return None


def _ocr_with_rapidocr(path: str) -> str | None:
    engine = _get_rapidocr()
    if engine is None:
        return None
    try:
        result, _ = engine(path)
    except Exception as exc:  # noqa: BLE001
        log.warning("image_routing: rapidocr call failed on %s: %s", path, exc)
        return None
    if not result:
        return ""  # ran OK, found no text
    lines: list[str] = []
    for row in result:
        if not row or len(row) < 2:
            continue
        txt = row[1]
        if isinstance(txt, str) and txt.strip():
            lines.append(txt.strip())
    return "\n".join(lines)


def _ocr_with_paddleocr(path: str) -> str | None:
    try:
        from paddleocr import PaddleOCR  # type: ignore
    except ImportError:
        return None
    except Exception as exc:  # noqa: BLE001
        log.debug("image_routing: paddleocr import failed: %s", exc)
        return None
    try:
        ocr = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)
        result = ocr.ocr(path, cls=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("image_routing: paddleocr call failed on %s: %s", path, exc)
        return None
    if not result:
        return ""
    lines: list[str] = []
    for page in result:
        if not page:
            continue
        for row in page:
            if not row or len(row) < 2:
                continue
            text_pair = row[1]
            if isinstance(text_pair, (list, tuple)) and text_pair:
                txt = str(text_pair[0] or "").strip()
            else:
                txt = str(text_pair or "").strip()
            if txt:
                lines.append(txt)
    return "\n".join(lines)


def _ocr_with_tesseract(path: str) -> str | None:
    try:
        import pytesseract  # type: ignore
        from PIL import Image  # type: ignore
    except ImportError:
        return None
    try:
        with Image.open(path) as img:
            try:
                text = pytesseract.image_to_string(img, lang="chi_sim+eng")
            except pytesseract.TesseractError:
                text = pytesseract.image_to_string(img)
        return (text or "").strip()
    except Exception as exc:  # noqa: BLE001
        log.warning("image_routing: tesseract call failed on %s: %s", path, exc)
        return None


def ocr_local(path: str) -> str | None:
    """Run local OCR on an image. Returns extracted text (possibly
    empty if image has no text), or ``None`` if every backend is
    unavailable / errored.

    Tries rapidocr → paddleocr → pytesseract. First success wins.
    """
    if not path or not os.path.isfile(path):
        return None
    for backend in (_ocr_with_rapidocr, _ocr_with_paddleocr, _ocr_with_tesseract):
        result = backend(path)
        if result is not None:
            return result
    return None


# ─── Auxiliary vision-model fallback (stub — future hook) ──────────

def _describe_via_aux_vision(path: str, config: dict[str, Any] | None) -> str | None:
    """Hook for a future cloud-vision describe-image path (Hermes's
    ``auxiliary.vision`` pattern). Today we lean on local OCR; this
    seam exists so when a user wants richer scene description than
    "text in the image", we can wire a vision-capable model here
    without re-plumbing the intake flow.

    Returns ``None`` until config has ``llm.auxiliary.vision`` set.
    """
    return None


# ─── Mode decision + enrichment (the only public surface) ──────────

def decide_image_mode(
    config: dict[str, Any] | None,
    profile_id: str | None,
) -> str:
    """``"native"`` if profile supports vision, else ``"text"``.

    Pure config; never inspects the model's actual behaviour.
    Add explicit ``supports_vision: false`` to the profile if a
    model claims vision but mishandles attachments in practice.
    """
    return "native" if supports_vision(config, profile_id) else "text"


def enrich_user_message(
    text: str | None,
    image_paths: Sequence[str],
    mode: str,
    *,
    config: dict[str, Any] | None = None,
) -> tuple[str, list[str]]:
    """Transform ``(text, image_paths)`` for the chosen mode.

    Returns ``(enriched_text, paths_to_pass_to_llm)``.

    ``native`` mode
        Keep the image paths intact (translator encodes them as
        vision blocks). Append a per-image ``[Image attached at:
        <path>]`` hint to the text so the model knows the on-disk
        location, e.g. for later tool calls.

    ``text`` mode
        OCR each image locally and prepend
        ``[image OCR | basename]\\n<text>`` blocks to the user
        text. **Drop** image paths from the returned list — the
        translator never sees pixels, so a text-only model never
        gets a payload it can't render.

        OCR failures degrade to ``[image: basename — OCR
        unavailable]`` so the model at least knows an image was
        attached but content couldn't be extracted; this is
        better than silent drop (the user would otherwise
        wonder why their screenshot was ignored).
    """
    base_text = (text or "").strip()
    paths = [p for p in image_paths if isinstance(p, str) and p]

    if not paths:
        return base_text, []

    if mode == "native":
        hints = "\n".join(f"[Image attached at: {p}]" for p in paths)
        body = base_text or "What do you see in this image?"
        return f"{body}\n\n{hints}", list(paths)

    # mode == "text"
    blocks: list[str] = []
    for p in paths:
        name = os.path.basename(p) or p
        ocr_text = ocr_local(p)
        if ocr_text is None:
            # Try aux vision fallback (currently always None).
            desc = _describe_via_aux_vision(p, config)
            if desc:
                blocks.append(f"[image description | {name}]\n{desc}")
            else:
                blocks.append(f"[image: {name} — OCR unavailable]")
        elif ocr_text == "":
            blocks.append(f"[image: {name} — no text detected]")
        else:
            blocks.append(f"[image OCR | {name}]\n{ocr_text}")

    prefix = "\n\n".join(blocks)
    if base_text:
        enriched = f"{prefix}\n\n{base_text}"
    else:
        enriched = prefix
    log.info(
        "image_routing: enriched user msg in text mode, "
        "images=%d ocr_chars=%d",
        len(paths), sum(len(b) for b in blocks),
    )
    return enriched, []  # paths dropped — translator gets no pixels


__all__ = [
    "supports_vision",
    "decide_image_mode",
    "enrich_user_message",
    "ocr_local",
]
