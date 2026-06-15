"""GenerateVideoToolProvider — ``generate_video`` tool.

Wraps :class:`xmclaw.providers.media.replicate_video.ReplicateVideoProvider`
(or future alternatives).
"""
from __future__ import annotations

import json
import time
from typing import Any

from xmclaw.core.ir import ToolCall, ToolResult, ToolSpec
from xmclaw.providers.tool.base import ToolProvider
from xmclaw.utils.log import get_logger

logger = get_logger(__name__)

_GENERATE_VIDEO_SPEC = ToolSpec(
    name="generate_video",
    description=(
        "Generate a short video from a text prompt (and optionally a "
        "starting image) using an AI video model. Returns the local "
        "file path and metadata.\n\n"
        "Use this when the user asks you to create, generate, or animate "
        "a video clip."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": (
                    "Detailed description of the video to generate. "
                    "Be specific about subject, motion, camera movement, "
                    "lighting, and mood."
                ),
            },
            "image_path": {
                "type": "string",
                "description": (
                    "Optional. Path to a starting image. Some models "
                    "require an image (image-to-video); others work "
                    "from text alone."
                ),
            },
            "duration": {
                "type": "integer",
                "description": (
                    "Optional target duration in seconds. Model may "
                    "ignore this and use its own fixed length."
                ),
            },
            "aspect_ratio": {
                "type": "string",
                "enum": ["16:9", "9:16", "1:1", "4:3"],
                "description": (
                    "Optional aspect ratio. Default depends on the model."
                ),
            },
        },
        "required": ["prompt"],
    },
    read_only=True,
)


class GenerateVideoToolProvider(ToolProvider):
    """Exposes ``generate_video``.

    Constructor params:

    * ``provider`` — a ``ReplicateVideoProvider`` instance (or any
      object with ``async generate(prompt, *, image_path, duration,
      aspect_ratio)``).
    * ``gate_check`` — optional fast/cheap LLM for prompt-safety gate.
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
        return [_GENERATE_VIDEO_SPEC]

    async def invoke(self, call: ToolCall) -> ToolResult:
        if call.name != "generate_video":
            return _fail(call, "unknown tool")
        if self._provider is None:
            return _fail(
                call,
                "generate_video is not configured — add a Replicate API "
                "token and a video model profile with capabilities "
                "['video_gen'] in daemon/config.json",
            )

        args = call.args or {}
        prompt = str(args.get("prompt", "")).strip()
        if not prompt:
            return _fail(call, "prompt (non-empty string) is required")

        image_path = str(args.get("image_path", "")).strip() or None
        duration_raw = args.get("duration")
        duration = int(duration_raw) if isinstance(duration_raw, int) and duration_raw > 0 else None
        aspect_ratio = str(args.get("aspect_ratio", "")).strip() or None

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
                logger.debug("generate_video.gate_failed: %s", exc)

        try:
            result = await self._provider.generate(
                prompt,
                image_path=image_path,
                duration=duration,
                aspect_ratio=aspect_ratio,
            )
        except ImportError as exc:
            return _fail(call, str(exc))
        except Exception as exc:  # noqa: BLE001
            return _fail(call, f"{type(exc).__name__}: {exc}")

        path = result.get("path", "")
        if not path:
            return _fail(call, "video generation succeeded but no file was produced")

        payload = json.dumps({
            "path": path,
            "output_url": result.get("output_url"),
            "model": result.get("model"),
        }, ensure_ascii=False)

        metadata: dict[str, Any] = {}
        metadata["attachments"] = [
            {"kind": "video", "path": path, "name": path.rsplit("/", 1)[-1]},
        ]

        return ToolResult(
            call_id=call.id, ok=True,
            content=payload,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
            side_effects=(f"generated video: {path}",),
            metadata=metadata,
        )

    @staticmethod
    async def _run_gate(gate_llm: Any, prompt: str) -> bool:
        from xmclaw.providers.llm.base import Message
        messages = [
            Message(
                role="system",
                content=(
                    "You are a safety gate. Reply ONLY 'yes' or 'no'. "
                    "Should an AI video model generate a video for this prompt? "
                    "Reply 'no' if the prompt requests illegal, harmful, "
                    "sexually explicit, or politically sensitive content."
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


__all__ = ["GenerateVideoToolProvider", "_GENERATE_VIDEO_SPEC"]
