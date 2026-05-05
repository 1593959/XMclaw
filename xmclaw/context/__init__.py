"""Long-conversation context management — compressor + helpers.

Public API:
  * ``ContextCompressor`` — 5-phase pipeline that prunes old tool
    results, summarises middle turns via an LLM, and protects the
    head + tail of the conversation. Ported from
    ``hermes-agent/agent/context_compressor.py``.

The compressor is owned by the AgentLoop (one instance per process,
shared across sessions). Per-session state (previous summary,
anti-thrashing counter) is keyed by ``session_id``.
"""
from __future__ import annotations

from xmclaw.context.compressor import (
    ContextCompressor,
    SUMMARY_PREFIX,
    estimate_messages_tokens_rough,
)
from xmclaw.context.tool_result_prune import (
    _summarize_tool_result,
    prune_old_tool_results,
)

__all__ = [
    "ContextCompressor",
    "SUMMARY_PREFIX",
    "estimate_messages_tokens_rough",
    "prune_old_tool_results",
    "_summarize_tool_result",
]
