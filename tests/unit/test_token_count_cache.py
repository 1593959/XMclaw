"""Unit tests for ContextCompressor token-count cache (2026-06-19).

Covers:
  1. Cache hit on identical message
  2. Multiple distinct messages coexist in cache
  3. estimate_incremental_tokens skips already-cached messages
  4. remove_messages_from_cache purges entries without leaks
  5. Accuracy parity with the original uncached function
  6. Performance: 100× repeated calc on 100 msgs is >10× faster with cache
"""
from __future__ import annotations

import random
import string
import time

import pytest

from xmclaw.context.compressor import (
    ContextCompressor,
    _count_tokens_in_message,
    _get_message_cache_key,
    estimate_messages_tokens_rough,
)
from xmclaw.core.ir.message import Message
from xmclaw.core.ir.toolcall import ToolCall


# ─── helpers ─────────────────────────────────────────────────────────

def _make_ascii_msg(length: int = 2_000) -> Message:
    text = "".join(random.choices(string.ascii_letters + string.digits, k=length))
    return Message(role="user", content=text)


def _make_cjk_msg(length: int = 2_000) -> Message:
    # CJK Unified range 4E00-9FFF
    text = "".join(chr(random.randint(0x4E00, 0x9FFF)) for _ in range(length))
    return Message(role="user", content=text)


def _make_msg_with_tool(length: int = 500) -> Message:
    text = "x" * length
    tc = ToolCall(
        name="test_tool",
        args={"query": "y" * 400, "nested": {"inner": "z" * 300}},
        provenance="synthetic",
    )
    return Message(role="assistant", content=text, tool_calls=(tc,))


def _make_msg_with_images(length: int = 100) -> Message:
    text = "a" * length
    return Message(role="user", content=text, images=("/tmp/1.png", "/tmp/2.png"))


# ─── 1. Cache hit on identical message ───────────────────────────────

def test_cache_hit_identical_message():
    cache: dict[str, int] = {}
    msg = _make_ascii_msg(100)

    t1 = _count_tokens_in_message(msg, cache=cache)
    t2 = _count_tokens_in_message(msg, cache=cache)

    assert t1 == t2
    assert len(cache) == 1


# ─── 2. Distinct messages coexist in cache ───────────────────────────

def test_cache_stores_multiple_messages():
    cache: dict[str, int] = {}
    msgs = [_make_ascii_msg(i * 100) for i in range(1, 6)]

    for m in msgs:
        _count_tokens_in_message(m, cache=cache)

    assert len(cache) == 5
    for m in msgs:
        key = _get_message_cache_key(m)
        assert key in cache


# ─── 3. Incremental tokens only for new messages ─────────────────────

def test_incremental_tokens_skips_cached():
    compressor = ContextCompressor(
        model="test", summarize_call=lambda _p, _t: None, quiet_mode=True
    )
    old_msgs = [_make_ascii_msg(200) for _ in range(5)]
    new_msgs = [_make_ascii_msg(300) for _ in range(3)]

    # prime cache with old messages
    compressor.estimate_messages_tokens_rough(old_msgs)
    cached_before = len(compressor._token_cache)

    # incremental should only count the 3 new ones
    incremental = compressor.estimate_incremental_tokens(new_msgs)
    cached_after = len(compressor._token_cache)

    assert cached_before == 5
    assert cached_after == 8
    assert incremental == sum(_count_tokens_in_message(m) for m in new_msgs)


# ─── 4. remove_messages_from_cache purges without leak ───────────────

def test_remove_from_cache_no_leak():
    compressor = ContextCompressor(
        model="test", summarize_call=lambda _p, _t: None, quiet_mode=True
    )
    msgs = [_make_ascii_msg(100) for _ in range(10)]
    compressor.estimate_messages_tokens_rough(msgs)
    assert len(compressor._token_cache) == 10

    removed = compressor.remove_messages_from_cache(msgs[:5])
    assert len(compressor._token_cache) == 5

    # removing same messages again returns 0
    removed_again = compressor.remove_messages_from_cache(msgs[:5])
    assert removed_again == 0


# ─── 5. Accuracy parity with uncached function ───────────────────────

def test_accuracy_parity_ascii():
    msgs = [_make_ascii_msg(1_000) for _ in range(20)]
    uncached = estimate_messages_tokens_rough(msgs)
    compressor = ContextCompressor(
        model="test", summarize_call=lambda _p, _t: None, quiet_mode=True
    )
    cached = compressor.estimate_messages_tokens_rough(msgs)
    assert cached == uncached


def test_accuracy_parity_cjk():
    msgs = [_make_cjk_msg(1_000) for _ in range(10)]
    uncached = estimate_messages_tokens_rough(msgs)
    compressor = ContextCompressor(
        model="test", summarize_call=lambda _p, _t: None, quiet_mode=True
    )
    cached = compressor.estimate_messages_tokens_rough(msgs)
    assert cached == uncached


def test_accuracy_parity_with_tools_and_images():
    msgs = [
        _make_ascii_msg(200),
        _make_cjk_msg(200),
        _make_msg_with_tool(300),
        _make_msg_with_images(50),
        Message(role="assistant", content="[GOAL-ANCHOR] skip me"),
    ]
    uncached = estimate_messages_tokens_rough(msgs)
    compressor = ContextCompressor(
        model="test", summarize_call=lambda _p, _t: None, quiet_mode=True
    )
    cached = compressor.estimate_messages_tokens_rough(msgs)
    assert cached == uncached


# ─── 6. Performance: cache hit >10× faster ───────────────────────────

def test_performance_cache_hit_10x():
    """100 messages × 2000 chars; repeated 100 times."""
    msgs = [_make_ascii_msg(2_000) for _ in range(100)]
    compressor = ContextCompressor(
        model="test", summarize_call=lambda _p, _t: None, quiet_mode=True
    )

    # warm cache
    compressor.estimate_messages_tokens_rough(msgs)
    assert len(compressor._token_cache) == 100

    # cached run (instance method reuses cache)
    t0 = time.perf_counter()
    for _ in range(100):
        compressor.estimate_messages_tokens_rough(msgs)
    t_cached = time.perf_counter() - t0

    # truly uncached run — module-level function with NO cache dict
    t0 = time.perf_counter()
    for _ in range(100):
        estimate_messages_tokens_rough(msgs)
    t_uncached = time.perf_counter() - t0

    ratio = t_uncached / t_cached if t_cached > 0 else float('inf')
    assert t_cached * 10 < t_uncached, (
        f"cached={t_cached:.4f}s uncached={t_uncached:.4f}s "
        f"ratio={ratio:.1f}×"
    )


# ─── 7. invalidate_token_cache clears everything ─────────────────────

def test_invalidate_clears_all():
    compressor = ContextCompressor(
        model="test", summarize_call=lambda _p, _t: None, quiet_mode=True
    )
    msgs = [_make_ascii_msg(100) for _ in range(5)]
    compressor.estimate_messages_tokens_rough(msgs)
    assert len(compressor._token_cache) == 5

    compressor.invalidate_token_cache()
    assert len(compressor._token_cache) == 0
