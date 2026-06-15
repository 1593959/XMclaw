"""GenerateImageToolProvider — ``generate_image`` tool.

Wraps :class:`xmclaw.providers.media.dalle3.Dalle3Provider` (or future
alternatives) as a ToolProvider so the agent can call it.
"""
from __future__ import annotations

import json
import time
from typing import Any

from xmclaw.core.ir import ToolCall, ToolResult, ToolSpec
from xmclaw.providers.tool.base import ToolProvider
from xmclaw.utils.log import get_logger

logger = get_logger(__name__)

_GENERATE_IMAGE_SPEC = ToolSpec(
    name="generate_image",
    description=(
        "Generate an image from a text prompt using an AI image model "
        "(e.g. DALL-E 3). Returns the local file path and metadata.\n\n"
        "Use this when the user asks you to draw, create, or generate "
        "an image, illustration, diagram, or photo."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": (
                    "Detailed description of the image to generate. "
                    "Be specific about subject, style, lighting, mood, "
                    "and composition."
                ),
            },
            "size": {
                "type": "string",
                "enum": ["1024x1024", "1792x1024", "1024x1792"],
                "description": (
                    "Image dimensions. 1024x1024 (square, default), "
                    "1792x1024 (landscape), 1024x1792 (portrait)."
                ),
            },
            "quality": {
                "type": "string",
                "enum": ["standard", "hd"],
                "description": (
                    "Image quality. 'standard' (default) is faster and "
                    "cheaper; 'hd' produces finer details and better "
                    "consistency."
                ),
            },
            "style": {
                "type": "string",
                "enum": ["vivid", "natural"],
                "description": (
                    "Style bias. 'vivid' (default) is hyper-real and "
                    "dramatic; 'natural' is less hyper-real."
                ),
            },
        },
        "required": ["prompt"],
    },
    read_only=True,
)


class GenerateImageToolProvider(ToolProvider):
    """Exposes ``generate_image``.

    Constructor params:

    * ``provider`` — a ``Dalle3Provider`` instance (or any object with
      ``async generate(prompt, *, size, quality, style)``).
    * ``gate_check`` — optional fast/cheap LLM to run a prompt-safety
      gate before spending money on image generation.  When set, the
      gate model is asked "should I generate this image?" and if it
      answers "no", the tool returns early with a refusal.
    """

    def __init__(
        self,
        *,
        provider: Any | None = None,
        gate_check: Any | None = None,
    ) -> None:
        self._provider = provider
        self._gate = gate_check

    def list_tools(self) -> list[ToolSpec]:
        return [_GENERATE_IMAGE_SPEC]

    async def invoke(self, call: ToolCall) -> ToolResult:
        if call.name != "generate_image":
            return _fail(call, "unknown tool")
        if self._provider is None:
            return _fail(
                call,
                "generate_image is not configured — add a DALL-E 3 "
                "profile with capabilities ['image_gen'] in daemon/config.json",
            )

        args = call.args or {}
        prompt = str(args.get("prompt", "")).strip()
        if not prompt:
            return _fail(call, "prompt (non-empty string) is required")

        size = str(args.get("size", "")).strip() or None
        quality = str(args.get("quality", "")).strip() or None
        style = str(args.get("style", "")).strip() or None

        t0 = time.perf_counter()

        # Optional gate check.
        if self._gate is not None:
            try:
                gate_ok = await self._run_gate(self._gate, prompt)
                if not gate_ok:
                    return ToolResult(
                        call_id=call.id, ok=False,
                        content=None,
                        error="Gate check refused this prompt (safety / policy)",
                        latency_ms=(time.perf_counter() - t0) * 1000.0,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.debug("generate_image.gate_failed: %s", exc)

        try:
            result = await self._provider.generate(
                prompt,
                size=size,
                quality=quality,
                style=style,
            )
        except ImportError as exc:
            return _fail(call, str(exc))
        except Exception as exc:  # noqa: BLE001
            return _fail(call, f"{type(exc).__name__}: {exc}")

        paths = result.get("paths") or []
        if not paths:
            return _fail(call, "image generation succeeded but no file was produced")

        payload = json.dumps({
            "paths": paths,
            "revised_prompt": (result.get("revised_prompts") or [""])[0],
            "size": result.get("size"),
            "quality": result.get("quality"),
            "style": result.get("style"),
        }, ensure_ascii=False)

        # B-Vision: attach the first image for automatic UI rendering.
        metadata: dict[str, Any] = {}
        if paths:
            metadata["attachments"] = [
                {"kind": "image", "path": paths[0], "name": paths[0].rsplit("/", 1)[-1]},
            ]

        return ToolResult(
            call_id=call.id, ok=True,
            content=payload,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
            side_effects=tuple(f"generated image: {p}" for p in paths),
            metadata=metadata,
        )

    @staticmethod
    async def _run_gate(gate_llm: Any, prompt: str) -> bool:
        """Ask a cheap model whether the prompt is safe to generate."""
        from xmclaw.providers.llm.base import Message
        messages = [
            Message(
                role="system",
                content=(
                    "You are a safety gate. Reply ONLY 'yes' or 'no'. "
                    "Should an AI image model generate an image for this prompt? "
                    "Reply 'no' if the prompt requests illegal, harmful, "
                    "sexually explicit, or politically sensitive imagery."
                ),
            ),
            Message(role="user", content=f"Prompt: {prompt}"),
        ]
        resp = await gate_llm.complete(messages)
        text = (getattr(resp, "content", "") or "").strip().lower()
        return text.startswith("yes") or "yes" in text[:10]


def _fail(call: ToolCall, error: str) -> ToolResult:
    return ToolResult(
        call_id=call.id, ok=False,
        content=None, error=error,
        latency_ms=0.0,
    )


__all__ = ["GenerateImageToolProvider", "_GENERATE_IMAGE_SPEC"]
