"""B-226 + P0-1 Phase-1: tool-result pruning, ported from Hermes
``context_compressor.py``.

Pre-B-226 XMclaw's _persist_history just dropped old turns when over
the message-count / token cap. Long conversations with big tool
results (file_read of a 50KB file, grep_files matching 200 lines, web_fetch
of an HTML page) burned context budget pointlessly: the model rarely
needs the full file content from 30 turns ago — a 1-line summary
("[file_read] read agent_loop.py — 87KB, 2400 lines") is enough.

This module preserves the most recent N tokens worth of tool results
intact, and replaces older ones with informative 1-line summaries.
Same algorithm as Hermes ``_prune_old_tool_results`` — just adapted
to XMclaw's Message + ToolCall dataclasses.

Three passes:
  1. **Dedup**: identical tool result content (same file read 5 times)
     keeps only the newest full copy; older duplicates → "[Duplicate]"
  2. **Summarize old**: tool messages outside protected tail get
     replaced with ``_summarize_tool_result(name, args, content)``
  3. **Truncate large args** (P0-1): assistant tool_calls with > 500 char
     args (file_write with 50KB content, apply_patch with a giant diff)
     get JSON-aware shrunk — long string values inside the JSON struct
     are clipped to 200 chars while the JSON shape stays valid. Without
     this a 50KB file_write call survives pruning entirely.

Hermes source: ``hermes-agent/agent/context_compressor.py:113-522``.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from typing import Any

# B-229 follow-up: relocated from ``xmclaw/utils/`` to
# ``xmclaw/context/`` so the runtime ``Message`` import satisfies the
# import-direction DAG. ``utils/`` cannot import from ``providers/``
# (utils is a leaf below providers); ``context/`` sits above providers
# and is allowed.
from xmclaw.providers.llm.base import Message

# Same default chars/4 ≈ token estimate as Hermes + agent_loop.
_CHARS_PER_TOKEN = 4

# Pass 3: tool_call argument truncation. Args longer than this get
# the JSON-aware shrink applied to long string leaves.
_TOOL_ARGS_TRUNCATE_THRESHOLD = 500
_TOOL_ARGS_LEAF_HEAD = 200


def _shrink_long_strings(obj: Any, head_chars: int = _TOOL_ARGS_LEAF_HEAD) -> Any:
    """Recursively clip long string leaves in a parsed JSON value.

    Keeps the JSON structure intact (all dict keys / list lengths
    preserved) so the result re-serialises into well-formed JSON.
    Non-string values (paths, ints, booleans, nulls) pass through.
    """
    if isinstance(obj, str):
        if len(obj) > head_chars:
            return obj[:head_chars] + "...[truncated]"
        return obj
    if isinstance(obj, dict):
        return {k: _shrink_long_strings(v, head_chars) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_shrink_long_strings(v, head_chars) for v in obj]
    return obj


def _truncate_tool_call_args(args: Any) -> tuple[Any, bool]:
    """Shrink long string values inside a tool-call args structure.

    Hermes ``_truncate_tool_call_args_json`` operates on a JSON string
    (OpenAI-style ``function.arguments``). XMclaw's ``ToolCall.args``
    is already a parsed dict, so we shrink the parsed structure
    directly. JSON-string args (rare) are parsed first.

    Returns ``(new_args, modified)``. ``modified=False`` means the
    args were under the threshold or non-shrinkable — caller should
    keep the original to avoid pointless rewrites.
    """
    if args is None:
        return args, False

    parsed: Any
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
        except (json.JSONDecodeError, TypeError):
            # Non-JSON tool args (some local backends use bare strings) —
            # leave alone. Rewriting them risks worse parsing downstream.
            if len(args) > _TOOL_ARGS_TRUNCATE_THRESHOLD:
                return args[:_TOOL_ARGS_LEAF_HEAD] + "...[truncated]", True
            return args, False
        # Round-trip via JSON so we serialise back as a string of the
        # same shape Hermes uses for the OpenAI tool-call format.
        original_str = args
    elif isinstance(args, (dict, list)):
        parsed = args
        original_str = json.dumps(args, ensure_ascii=False)
    else:
        return args, False

    if len(original_str) <= _TOOL_ARGS_TRUNCATE_THRESHOLD:
        return args, False

    shrunken = _shrink_long_strings(parsed)
    if isinstance(args, str):
        # ensure_ascii=False preserves CJK/emoji rather than \uXXXX
        return json.dumps(shrunken, ensure_ascii=False), True
    return shrunken, True


def _summarize_tool_result(name: str, args: Any, content: str) -> str:
    """Generate a 1-line human-readable summary of a tool call+result.

    Tool name dispatch covers XMclaw's builtin tool roster.
    Falls back to ``[name] (N chars)`` for unknown tools.

    Examples:
        [bash] ran `xmclaw stop && xmclaw start` -> 23 lines output
        [file_read] read agent_loop.py from line 1 (87,432 chars)
        [grep_files] pattern='thinking' in xmclaw/ -> 14 matches
    """
    if isinstance(args, str):
        try:
            args_dict = json.loads(args)
        except (json.JSONDecodeError, TypeError):
            args_dict = {}
    elif isinstance(args, dict):
        args_dict = args
    else:
        args_dict = {}

    content = content or ""
    n_chars = len(content)
    n_lines = content.count("\n") + 1 if content.strip() else 0

    if name == "bash":
        cmd = str(args_dict.get("command", "") or args_dict.get("cmd", ""))
        if len(cmd) > 80:
            cmd = cmd[:77] + "..."
        # Try to peel an exit_code out of the JSON-shaped tool result.
        m = re.search(r'"exit_code"\s*:\s*(-?\d+)', content)
        exit_code = m.group(1) if m else "?"
        return f"[bash] ran `{cmd}` -> exit {exit_code}, {n_lines} lines output"

    if name == "file_read":
        path = args_dict.get("path") or args_dict.get("file_path") or "?"
        offset = args_dict.get("offset") or args_dict.get("start_line") or 1
        return f"[file_read] read {path} from line {offset} ({n_chars:,} chars)"

    if name == "file_write":
        path = args_dict.get("path") or args_dict.get("file_path") or "?"
        body = str(args_dict.get("content") or "")
        wrote_lines = body.count("\n") + 1 if body else "?"
        return f"[file_write] wrote {path} ({wrote_lines} lines)"

    if name == "apply_patch":
        path = args_dict.get("path") or args_dict.get("file_path") or "?"
        return f"[apply_patch] patched {path} ({n_chars:,} chars result)"

    if name == "file_delete":
        path = args_dict.get("path") or args_dict.get("file_path") or "?"
        return f"[file_delete] removed {path}"

    if name == "list_dir":
        path = args_dict.get("path") or "?"
        m = re.search(r'"count"\s*:\s*(\d+)', content)
        count = m.group(1) if m else "?"
        return f"[list_dir] {path} -> {count} entries"

    if name == "glob_files":
        pattern = args_dict.get("pattern") or "?"
        m = re.search(r'"count"\s*:\s*(\d+)', content)
        count = m.group(1) if m else "?"
        return f"[glob_files] '{pattern}' -> {count} matches"

    if name == "grep_files":
        pattern = args_dict.get("pattern") or args_dict.get("query") or "?"
        path = args_dict.get("path") or "."
        m = re.search(r'"total_matches"\s*:\s*(\d+)', content)
        count = m.group(1) if m else "?"
        return f"[grep_files] '{pattern}' in {path} -> {count} matches"

    if name == "web_fetch":
        url = args_dict.get("url") or "?"
        return f"[web_fetch] {url} ({n_chars:,} chars)"

    if name == "web_search":
        q = args_dict.get("query") or "?"
        return f"[web_search] '{q}' ({n_chars:,} chars result)"

    if name == "memory_search":
        q = args_dict.get("query") or "?"
        kind = args_dict.get("kind") or "all"
        m = re.search(r'"hits_count"\s*:\s*(\d+)', content)
        count = m.group(1) if m else "?"
        return f"[memory_search] kind={kind} '{q}' -> {count} hits"

    if name == "sqlite_query":
        db = args_dict.get("db") or "?"
        m = re.search(r'"row_count"\s*:\s*(\d+)', content)
        count = m.group(1) if m else "?"
        return f"[sqlite_query] db={db} -> {count} rows"

    if name and name.startswith("skill_"):
        return f"[{name}] ({n_chars:,} chars result)"

    # Unknown tool — minimal generic summary.
    return f"[{name or 'tool'}] ({n_chars:,} chars)"


def prune_old_tool_results(
    messages: list[Message],
    *,
    protect_tail_tokens: int = 6000,
    protect_tail_count_floor: int = 6,
) -> tuple[list[Message], int]:
    """B-226: replace older tool results with 1-line summaries.

    Walks backward from the end protecting the most recent messages
    that fall within ``protect_tail_tokens``; everything older with
    a tool result > 200 chars gets summarised. Identical content
    (same file read N times) gets deduplicated to keep only the
    newest full copy.

    Returns ``(new_messages, pruned_count)`` — caller emits a
    CONTEXT_COMPRESSED event when pruned > 0.
    """
    if not messages:
        return list(messages), 0

    # Build call_id → (tool_name, args) lookup from assistant messages
    # that emitted tool_calls. We need name+args to write a useful
    # summary string when we hit the matching tool message later.
    call_lookup: dict[str, tuple[str, Any]] = {}
    for m in messages:
        if m.role != "assistant":
            continue
        for tc in m.tool_calls or ():
            cid = getattr(tc, "id", None) or ""
            if not cid:
                continue
            call_lookup[cid] = (
                getattr(tc, "name", "unknown") or "unknown",
                getattr(tc, "args", {}) or {},
            )

    # Determine the prune boundary by walking BACKWARD accumulating
    # tokens. Protected tail = newest messages within the budget.
    accumulated = 0
    boundary = len(messages)
    floor = min(protect_tail_count_floor, max(0, len(messages) - 1))
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        msg_tokens = len(m.content or "") // _CHARS_PER_TOKEN + 10
        for tc in m.tool_calls or ():
            args_str = (
                json.dumps(getattr(tc, "args", {}) or {}, ensure_ascii=False)
                if getattr(tc, "args", None) is not None else ""
            )
            msg_tokens += len(args_str) // _CHARS_PER_TOKEN
        if (
            accumulated + msg_tokens > protect_tail_tokens
            and (len(messages) - i) >= floor
        ):
            boundary = i
            break
        accumulated += msg_tokens
        boundary = i
    prune_boundary = max(boundary, len(messages) - floor)

    result = list(messages)
    pruned = 0

    # Pass 1: dedupe identical tool result content (newest wins).
    # Same 50KB file_read repeated 5 times across a long session →
    # keep only the most recent full copy; older copies become a
    # 1-line back-reference. Caps message size before pass-2 even
    # has to look at them.
    seen_hashes: dict[str, int] = {}
    for i in range(len(result) - 1, -1, -1):
        m = result[i]
        if m.role != "tool":
            continue
        content = m.content or ""
        if not isinstance(content, str) or len(content) < 200:
            continue
        h = hashlib.md5(
            content.encode("utf-8", errors="replace"),
        ).hexdigest()[:12]
        if h in seen_hashes:
            result[i] = dataclasses.replace(
                m,
                content="[Duplicate tool output — same content as a more recent call]",
            )
            pruned += 1
        else:
            seen_hashes[h] = i

    # Pass 2: replace old tool results outside protected tail with
    # a 1-line summary derived from the matching tool_call.
    for i in range(prune_boundary):
        m = result[i]
        if m.role != "tool":
            continue
        content = m.content or ""
        if not isinstance(content, str) or len(content) <= 200:
            continue
        if content.startswith("[Duplicate tool output"):
            continue
        cid = m.tool_call_id or ""
        name, args = call_lookup.get(cid, ("unknown", {}))
        summary = _summarize_tool_result(name, args, content)
        result[i] = dataclasses.replace(m, content=summary)
        pruned += 1

    # Pass 3 (P0-1): JSON-aware truncation of large tool_call args in
    # old assistant messages. A single 50KB file_write or apply_patch
    # call survives passes 1+2 entirely without this — Pass 2 only
    # touches role=tool RESULT messages, not the assistant tool_call
    # that emitted them. Walk the prune zone and shrink long string
    # leaves inside ``ToolCall.args`` while keeping the JSON shape
    # valid (downstream providers 400 on malformed args).
    for i in range(prune_boundary):
        m = result[i]
        if m.role != "assistant" or not m.tool_calls:
            continue
        new_calls: list[Any] = []
        modified = False
        for tc in m.tool_calls:
            args = getattr(tc, "args", None)
            shrunken, did_shrink = _truncate_tool_call_args(args)
            if did_shrink:
                new_calls.append(dataclasses.replace(tc, args=shrunken))
                modified = True
            else:
                new_calls.append(tc)
        if modified:
            result[i] = dataclasses.replace(m, tool_calls=tuple(new_calls))

    return result, pruned


__all__ = ["prune_old_tool_results", "_summarize_tool_result"]
