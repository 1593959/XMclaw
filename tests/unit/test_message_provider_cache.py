"""Tests for Message provider-dict caching and ToolCall args JSON caching.

Reference: 2026-06-19 provider-cache implementation — per-message dict
conversion is cached on the Message object to avoid re-building the
same dict list across hops within a turn.
"""
from __future__ import annotations

import time
from typing import Any

import pytest

from xmclaw.core.ir import Message, ToolCall
from xmclaw.providers.llm.anthropic import AnthropicLLM
from xmclaw.providers.llm.openai import OpenAILLM


# ── helpers ───────────────────────────────────────────────────────────────


def _make_messages(n: int) -> list[Message]:
    """Return a diverse list of n messages."""
    messages: list[Message] = []
    for i in range(n):
        role = ["system", "user", "assistant", "tool"][i % 4]
        if role == "system":
            messages.append(Message(role="system", content=f"sys {i}"))
        elif role == "user":
            messages.append(Message(role="user", content=f"user {i}"))
        elif role == "assistant":
            tc = ToolCall(
                name="bash",
                args={"cmd": f"ls {i}"},
                provenance="synthetic",
                id=f"tc-{i}",
            )
            messages.append(
                Message(role="assistant", content=f"assist {i}", tool_calls=(tc,))
            )
        else:
            messages.append(
                Message(role="tool", content=f"result {i}", tool_call_id=f"tc-{i-1}")
            )
    return messages


# ── Message.to_provider_dict ────────────────────────────────────────────


def test_second_conversion_uses_cache() -> None:
    """The same message converted twice: second call is a cache hit."""
    msg = Message(role="user", content="hello")

    call_count = 0

    def _compute() -> dict:
        nonlocal call_count
        call_count += 1
        return {"role": "user", "content": "hello"}

    # First call: compute
    r1 = msg.to_provider_dict("test", _compute)
    assert call_count == 1
    assert r1 == {"role": "user", "content": "hello"}

    # Second call: cache hit
    r2 = msg.to_provider_dict("test", _compute)
    assert call_count == 1  # compute NOT called again
    assert r2 == r1

    # But the returned dict is a copy, not the same object
    assert r2 is not r1


def test_different_provider_keys_are_separate() -> None:
    """Cache entries for different providers are isolated."""
    msg = Message(role="assistant", content="hi")

    calls: dict[str, int] = {}

    def _make_compute(key: str) -> Any:
        def _compute() -> dict:
            calls[key] = calls.get(key, 0) + 1
            return {"role": "assistant", "content": "hi", "provider": key}

        return _compute

    r1 = msg.to_provider_dict("anthropic", _make_compute("anthropic"))
    r2 = msg.to_provider_dict("openai", _make_compute("openai"))

    assert calls["anthropic"] == 1
    assert calls["openai"] == 1
    assert r1["provider"] == "anthropic"
    assert r2["provider"] == "openai"

    # Second anthropic call hits cache
    r3 = msg.to_provider_dict("anthropic", _make_compute("anthropic"))
    assert calls["anthropic"] == 1
    assert r3["provider"] == "anthropic"


def test_different_messages_cache_independently() -> None:
    """Each Message has its own cache."""
    m1 = Message(role="user", content="a")
    m2 = Message(role="user", content="b")

    counter = 0

    def _compute() -> dict:
        nonlocal counter
        counter += 1
        return {"role": "user", "content": "computed"}

    m1.to_provider_dict("x", _compute)
    m2.to_provider_dict("x", _compute)
    assert counter == 2

    # Both are now cached
    m1.to_provider_dict("x", _compute)
    m2.to_provider_dict("x", _compute)
    assert counter == 2


def test_cache_hit_rate_over_5_conversions() -> None:
    """5 conversions of the same message: 1 miss, 4 hits."""
    msg = Message(role="user", content="cache me")

    counter = 0

    def _compute() -> dict:
        nonlocal counter
        counter += 1
        return {"role": "user", "content": "cache me"}

    for _ in range(5):
        msg.to_provider_dict("demo", _compute)

    assert counter == 1
    assert len(msg._provider_dict_cache) == 1
    assert "demo" in msg._provider_dict_cache


def test_cached_result_is_identical_to_fresh() -> None:
    """A cached conversion produces the same output as a fresh one."""
    msg = Message(
        role="assistant",
        content="using tools",
        tool_calls=(
            ToolCall(name="bash", args={"cmd": "ls"}, provenance="synthetic"),
        ),
    )

    fresh = msg.to_provider_dict(
        "fresh",
        lambda: {
            "role": "assistant",
            "content": "using tools",
            "tool_calls": [
                {
                    "type": "tool_use",
                    "id": msg.tool_calls[0].id,
                    "name": "bash",
                    "input": {"cmd": "ls"},
                },
            ],
        },
    )

    # The cache now has the entry
    cached = msg.to_provider_dict(
        "fresh",
        lambda: {
            "role": "assistant",
            "content": "WRONG",  # never called because cache is hit
        },
    )

    assert cached == fresh
    assert cached is not fresh  # Different object (copy)


def test_copy_protects_cache_from_mutation() -> None:
    """Post-processing on the returned dict must not pollute the cache."""
    msg = Message(role="user", content="hello")

    def _compute() -> dict:
        return {
            "role": "user",
            "content": "hello",
            "content_blocks": [{"type": "text", "text": "hello"}],
        }

    r1 = msg.to_provider_dict("p", _compute)
    # Mutate the returned dict (simulating provider post-processing)
    r1["content_blocks"][0]["cache_control"] = {"type": "ephemeral"}

    # Second call should get a clean copy, not the mutated one
    r2 = msg.to_provider_dict("p", _compute)
    assert "cache_control" not in r2["content_blocks"][0]


# ── Provider integration ─────────────────────────────────────────────────


def test_anthropic_messages_cached_across_hops() -> None:
    """AnthropicLLM._messages_to_anthropic populates per-message cache."""
    tc = ToolCall(
        name="bash", args={"cmd": "ls"}, provenance="synthetic", id="tc-1"
    )
    msgs = [
        Message(role="system", content="you are a helper"),
        Message(role="user", content="hi"),
        Message(role="assistant", content="ok", tool_calls=(tc,)),
        Message(role="tool", content="done", tool_call_id="tc-1"),
    ]

    # First call: compute
    system1, converted1 = AnthropicLLM._messages_to_anthropic(msgs)

    # Check that non-system messages have cache entries
    assert "anthropic" in msgs[1]._provider_dict_cache
    assert "anthropic" in msgs[2]._provider_dict_cache
    assert "anthropic" in msgs[3]._provider_dict_cache

    # System message is not stored in per-message cache (handled separately)
    assert "anthropic" not in msgs[0]._provider_dict_cache

    # Second call: cache hit
    system2, converted2 = AnthropicLLM._messages_to_anthropic(msgs)
    assert converted1 == converted2
    assert system1 == system2


def test_openai_messages_cached_across_hops() -> None:
    """OpenAILLM._messages_to_openai populates per-message cache."""
    tc = ToolCall(
        name="bash", args={"cmd": "ls"}, provenance="synthetic", id="tc-1"
    )
    msgs = [
        Message(role="system", content="you are a helper"),
        Message(role="user", content="hi"),
        Message(role="assistant", content="ok", tool_calls=(tc,)),
        Message(role="tool", content="done", tool_call_id="tc-1"),
    ]

    # First call
    out1 = OpenAILLM._messages_to_openai(msgs)

    # Check non-system messages are cached
    assert "openai" in msgs[1]._provider_dict_cache
    assert "openai" in msgs[2]._provider_dict_cache
    assert "openai" in msgs[3]._provider_dict_cache

    # System messages are handled directly, not cached via to_provider_dict
    assert "openai" not in msgs[0]._provider_dict_cache

    # Second call
    out2 = OpenAILLM._messages_to_openai(msgs)
    assert out1 == out2


def test_openai_system_message_not_cached() -> None:
    """System messages in OpenAI are handled directly (not cached)."""
    msgs = [Message(role="system", content="sys")]
    out = OpenAILLM._messages_to_openai(msgs)
    assert out[0]["role"] == "system"
    assert "openai" not in msgs[0]._provider_dict_cache


def test_anthropic_post_processing_does_not_pollute_cache() -> None:
    """Cache breakpoint injection on the last message must not pollute the cache."""
    msgs = [
        Message(role="system", content="sys"),
        Message(role="user", content="hi"),
    ]
    _, converted1 = AnthropicLLM._messages_to_anthropic(msgs)

    # The last message should have cache_control in the output
    last1 = converted1[-1]
    assert last1["content"][-1].get("cache_control") == {"type": "ephemeral"}

    # Second call: the cache entry should NOT have cache_control
    _, converted2 = AnthropicLLM._messages_to_anthropic(msgs)
    last2 = converted2[-1]
    assert last2["content"][-1].get("cache_control") == {"type": "ephemeral"}

    # The cached canonical version should NOT have cache_control
    cached_canonical = msgs[1]._provider_dict_cache["anthropic"]
    assert cached_canonical == {"role": "user", "content": "hi"}


# ── Performance ───────────────────────────────────────────────────────────


def test_performance_50_messages_5_hops() -> None:
    """50 messages × 5 hops: cached conversion should be at least 5× faster."""
    messages = _make_messages(50)

    # Populate cache (first hop)
    t0 = time.perf_counter()
    AnthropicLLM._messages_to_anthropic(messages)
    t_first = time.perf_counter() - t0

    # One cache-hit hop
    t0 = time.perf_counter()
    AnthropicLLM._messages_to_anthropic(messages)
    t_hit = time.perf_counter() - t0

    speedup = t_first / t_hit if t_hit > 0 else float("inf")
    assert speedup >= 5.0, (
        f"Expected 5×+ speedup, got {speedup:.2f}x "
        f"(full: {t_first:.4f}s, cached: {t_hit:.4f}s)"
    )


def test_performance_50_messages_5_hops_total() -> None:
    """Total time for 5 hops (1 miss + 4 hits) vs 5 full conversions.

    Plain-text message conversion is sub-millisecond, so a single-shot
    wall-clock comparison is noise-dominated (GC + scheduler jitter on a
    ~0.5ms region flips the ratio run-to-run). Use a min-of-N micro-benchmark
    with GC disabled during timing — the minimum is the noise-free floor —
    and pre-build all fresh lists OUTSIDE the timed region so we measure
    conversion, not Message construction.
    """
    import gc

    samples = 9
    cached_messages = _make_messages(50)
    AnthropicLLM._messages_to_anthropic(cached_messages)  # warm the cache

    t_cached_total = float("inf")
    for _ in range(samples):
        gc.disable()
        t0 = time.perf_counter()
        for _ in range(5):
            AnthropicLLM._messages_to_anthropic(cached_messages)
        t_cached_total = min(t_cached_total, time.perf_counter() - t0)
        gc.enable()

    # One fresh batch (5 uncached lists) per sample, all built up front.
    fresh_batches = [[_make_messages(50) for _ in range(5)] for _ in range(samples)]
    t_fresh_total = float("inf")
    for batch in fresh_batches:
        gc.disable()
        t0 = time.perf_counter()
        for fresh in batch:
            AnthropicLLM._messages_to_anthropic(fresh)
        t_fresh_total = min(t_fresh_total, time.perf_counter() - t0)
        gc.enable()

    speedup = t_fresh_total / t_cached_total if t_cached_total > 0 else float("inf")
    assert speedup >= 3.0, (
        f"Expected 3×+ total speedup, got {speedup:.2f}x "
        f"(fresh 5 hops: {t_fresh_total:.4f}s, cached 5 hops: {t_cached_total:.4f}s)"
    )


# ── ToolCall args JSON caching ───────────────────────────────────────────


def test_toolcall_args_json_caches() -> None:
    """ToolCall.args_json() caches the JSON string after first call."""
    tc = ToolCall(
        name="bash", args={"cmd": "ls", "dir": "/tmp"}, provenance="synthetic"
    )

    j1 = tc.args_json()
    j2 = tc.args_json()

    assert j1 == j2
    assert j1 == '{"cmd": "ls", "dir": "/tmp"}'
    assert tc._args_json_cache == j1

    # The returned string is the same object (cached)
    assert j1 is j2


def test_toolcall_args_json_different_args_different_json() -> None:
    """Different args produce different JSON strings."""
    tc1 = ToolCall(name="bash", args={"cmd": "ls"}, provenance="synthetic")
    tc2 = ToolCall(name="bash", args={"cmd": "pwd"}, provenance="synthetic")

    assert tc1.args_json() != tc2.args_json()


def test_openai_translator_uses_cached_args_json() -> None:
    """OpenAI translator encode_to_provider uses the cached args_json."""
    from xmclaw.providers.llm.translators import openai_tool_shape as translator

    tc = ToolCall(name="bash", args={"cmd": "ls"}, provenance="synthetic")

    # First call populates cache
    out1 = translator.encode_to_provider(tc)
    assert out1["function"]["arguments"] == '{"cmd": "ls"}'

    # Second call uses cached args_json (same object)
    out2 = translator.encode_to_provider(tc)
    assert out2 == out1
    assert out2["function"]["arguments"] is out1["function"]["arguments"]
