"""Message IR — single chat-message dataclass shared across the daemon.

2026-05-18: extracted from ``xmclaw/providers/llm/base.py``. Lives
under ``core/ir/`` so the ``core/`` packages
(``core/evolution/reflective_mutator``,
``core/journal/strategy_distiller``, ``cognition/planner``,
``cognition/reasoning``, etc.) can build ``Message`` instances when
they need to call into a duck-typed LLM, without reaching back into
``providers/`` and violating the
"core cannot import from providers or skills" architectural rule
(scripts/check_import_direction.py).

``providers/llm/base.py`` continues to ``from xmclaw.core.ir import
Message`` and re-export it, so the existing
``from xmclaw.providers.llm.base import Message`` import path that
~40 call sites use still works — same object, no copy.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from xmclaw.core.ir.toolcall import ToolCall


@dataclass(frozen=True, slots=True)
class Message:
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None  # for role=tool
    # B-Vision: image attachments for user-role messages. Each entry
    # is either a local file path (translator reads + base64-encodes)
    # or a ``data:`` URL (passes through). Used by the computer-use
    # loop — after ``screen_capture``, hop_loop injects a synthetic
    # user message with the screenshot here so the LLM SEES the
    # screen instead of squinting at OCR text. Ignored for non-user
    # roles. Empty tuple = plain text-only message (default,
    # backwards-compatible).
    images: tuple[str, ...] = ()
    # 2026-05-26: extended-thinking / reasoning text. Set on
    # assistant-role messages when the prior LLM hop emitted a
    # thinking block (Anthropic) or ``reasoning_content`` (DeepSeek
    # V4 / MiniMax / Moonshot reasoning models). The translator
    # re-emits it on the next hop so the model sees its own prior
    # reasoning — DeepSeek-V4 thinking mode 400s without this echo.
    # Empty string for non-thinking turns / providers.
    thinking: str = ""
    thinking_signature: str = ""
    # Provider-dict cache: key = provider name ("anthropic" / "openai"),
    # value = the canonical (un-mutated) dict produced by per-message
    # conversion.  Post-processing (cache_control injection, tool-pairing
    # repair) is done on *copies* so the canonical entry stays clean.
    _provider_dict_cache: dict[str, dict] = field(
        default_factory=dict, repr=False, compare=False, hash=False,
    )

    def to_provider_dict(self, provider_name: str, compute: Callable[[], dict]) -> dict:
        """Return a fresh copy of the provider-specific dict for this message.

        ``compute`` is called only on the first request for a given
        ``provider_name``.  The result is stored in
        ``_provider_dict_cache`` and returned on every subsequent call.
        The returned dict is always a copy so provider-level post-
        processing (e.g. cache_control injection) cannot pollute the
        cached canonical version.
        """
        cache = self._provider_dict_cache
        cached = cache.get(provider_name)
        if cached is not None:
            return _copy_message_dict(cached)
        result = compute()
        cache[provider_name] = result
        return _copy_message_dict(result)


def _copy_message_dict(d: dict) -> dict:
    """Shallow-ish copy of a provider message dict.

    Copies the top-level dict and any nested block lists one level deep
    so provider post-processing (cache_control injection, tool-pairing
    repair) cannot mutate the cached canonical version.
    """
    out = dict(d)
    if not any(isinstance(v, list) for v in out.values()):
        return out
    for key, val in out.items():
        if isinstance(val, list):
            out[key] = [
                dict(b) if isinstance(b, dict) else b
                for b in val
            ]
    return out


__all__ = ["Message"]
