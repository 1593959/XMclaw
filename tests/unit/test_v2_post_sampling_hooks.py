"""B-112: post-sampling hook framework + ExtractMemoriesHook.

Pins:
  * HookRegistry.dispatch fires every enabled hook concurrently
  * disabled hooks are skipped
  * a hook that raises does NOT abort the chain
  * ExtractMemoriesHook respects ``extract_memories.enabled`` gate
  * extracted facts land under ## Auto-extracted in MEMORY.md
  * empty / malformed LLM response → no write
"""
from __future__ import annotations

from pathlib import Path

import pytest

from xmclaw.daemon.post_sampling_hooks import (
    ExtractMemoriesHook,
    HookContext,
    HookRegistry,
    PostSamplingHook,
)


class _StubLLM:
    def __init__(self, response: str) -> None:
        self._r = response
        self.call_count = 0

    async def complete(self, messages, tools=None):
        self.call_count += 1

        class _Resp:
            def __init__(self, c: str) -> None:
                self.content = c

        return _Resp(self._r)


def _ctx(persona_dir, cfg=None, llm=None) -> HookContext:
    return HookContext(
        session_id="s",
        agent_id="main",
        user_message="hi",
        assistant_response="hello",
        history=[],
        llm=llm or _StubLLM('{"facts": []}'),
        persona_dir=persona_dir,
        cfg=cfg or {},
    )


# ── HookRegistry ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_fires_enabled_hooks(tmp_path: Path) -> None:
    fired: list[str] = []

    class _A(PostSamplingHook):
        id = "a"
        async def run(self, ctx): fired.append("a")

    class _B(PostSamplingHook):
        id = "b"
        async def run(self, ctx): fired.append("b")

    reg = HookRegistry()
    reg.register(_A())
    reg.register(_B())
    await reg.dispatch(_ctx(tmp_path))
    assert sorted(fired) == ["a", "b"]


@pytest.mark.asyncio
async def test_dispatch_skips_disabled_hooks(tmp_path: Path) -> None:
    fired: list[str] = []

    class _A(PostSamplingHook):
        id = "a"
        def is_enabled(self, ctx): return False
        async def run(self, ctx): fired.append("a")

    class _B(PostSamplingHook):
        id = "b"
        async def run(self, ctx): fired.append("b")

    reg = HookRegistry()
    reg.register(_A())
    reg.register(_B())
    await reg.dispatch(_ctx(tmp_path))
    assert fired == ["b"]


@pytest.mark.asyncio
async def test_one_hook_raising_doesnt_break_others(tmp_path: Path) -> None:
    fired: list[str] = []

    class _Boom(PostSamplingHook):
        id = "boom"
        async def run(self, ctx): raise RuntimeError("kaboom")

    class _Good(PostSamplingHook):
        id = "good"
        async def run(self, ctx): fired.append("good")

    reg = HookRegistry()
    reg.register(_Boom())
    reg.register(_Good())
    await reg.dispatch(_ctx(tmp_path))
    assert fired == ["good"]


# ── ExtractMemoriesHook ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_extract_disabled_when_config_off(tmp_path: Path) -> None:
    hook = ExtractMemoriesHook()
    ctx = _ctx(tmp_path, cfg={})
    assert hook.is_enabled(ctx) is False


@pytest.mark.asyncio
async def test_extract_disabled_without_persona_dir() -> None:
    hook = ExtractMemoriesHook()
    ctx = _ctx(None, cfg={"evolution": {"memory": {"extract_memories": {"enabled": True}}}})
    assert hook.is_enabled(ctx) is False


@pytest.mark.asyncio
async def test_extract_writes_facts_when_enabled(tmp_path: Path) -> None:
    """Happy path — LLM returns a fact list, hook appends to MEMORY.md
    under the auto-extracted section."""
    llm = _StubLLM('{"facts": ["user prefers tabs over spaces"]}')
    cfg = {"evolution": {"memory": {"extract_memories": {"enabled": True}}}}
    ctx = _ctx(tmp_path, cfg=cfg, llm=llm)
    hook = ExtractMemoriesHook()
    assert hook.is_enabled(ctx) is True
    await hook.run(ctx)
    assert llm.call_count == 1
    body = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    assert "## Auto-extracted" in body
    assert "user prefers tabs over spaces" in body


@pytest.mark.asyncio
async def test_extract_empty_facts_no_write(tmp_path: Path) -> None:
    llm = _StubLLM('{"facts": []}')
    cfg = {"evolution": {"memory": {"extract_memories": {"enabled": True}}}}
    ctx = _ctx(tmp_path, cfg=cfg, llm=llm)
    hook = ExtractMemoriesHook()
    await hook.run(ctx)
    assert not (tmp_path / "MEMORY.md").exists()


@pytest.mark.asyncio
async def test_extract_malformed_llm_response_no_write(tmp_path: Path) -> None:
    llm = _StubLLM("not even json")
    cfg = {"evolution": {"memory": {"extract_memories": {"enabled": True}}}}
    ctx = _ctx(tmp_path, cfg=cfg, llm=llm)
    hook = ExtractMemoriesHook()
    await hook.run(ctx)
    assert not (tmp_path / "MEMORY.md").exists()
