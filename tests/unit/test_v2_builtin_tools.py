"""BuiltinTools (file_read / file_write) — unit tests.

Covers: happy paths, structured-error paths, side_effects population,
allowlist enforcement. Every failure mode must return a structured
ToolResult with ``ok=False`` and an ``error`` message — the orchestrator
inspects those, never swallowed exceptions.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from xmclaw.core.ir import ToolCall
from xmclaw.providers.tool.builtin import BuiltinTools


def _call(name: str, args: dict) -> ToolCall:
    return ToolCall(name=name, args=args, provenance="synthetic")


# ── list_tools ────────────────────────────────────────────────────────────

def test_list_tools_default_roster() -> None:
    """Default posture: all tool families on. Must include filesystem
    + shell + web + todo tools. Permissions default to MAXIMUM."""
    tools = BuiltinTools().list_tools()
    names = {t.name for t in tools}
    assert {"file_read", "file_write", "list_dir"} <= names
    assert "bash" in names
    assert {"web_fetch", "web_search"} <= names
    # Todo tools ship by default -- no kill-switch; they're pure
    # in-memory state with no side effects.
    assert {"todo_write", "todo_read"} <= names


def test_list_tools_kill_switches() -> None:
    """enable_bash/enable_web False must remove those tools from the spec."""
    names_no_bash = {
        t.name for t in BuiltinTools(enable_bash=False).list_tools()
    }
    assert "bash" not in names_no_bash
    assert {"file_read", "web_fetch"} <= names_no_bash

    names_no_web = {
        t.name for t in BuiltinTools(enable_web=False).list_tools()
    }
    assert "web_fetch" not in names_no_web
    assert "web_search" not in names_no_web
    assert "bash" in names_no_web


def test_list_tools_schemas_well_formed() -> None:
    """Every spec has an object parameters_schema. todo_read takes no
    args (required=[]); everything else has at least one required field."""
    for spec in BuiltinTools().list_tools():
        assert spec.parameters_schema["type"] == "object"
        if spec.name == "todo_read":
            # Legitimately zero-arg -- just reads current state.
            continue
        assert len(spec.parameters_schema.get("required", [])) >= 1, (
            f"{spec.name} has no required fields"
        )


# ── file_read happy path ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_file_read_returns_content_and_empty_side_effects() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "x.txt"
        p.write_text("hello world", encoding="utf-8")
        tools = BuiltinTools(allowed_dirs=[tmp])
        result = await tools.invoke(_call("file_read", {"path": str(p)}))
        assert result.ok is True
        assert result.content == "hello world"
        assert result.side_effects == ()  # pure read — nothing to verify
        assert result.latency_ms >= 0.0


@pytest.mark.asyncio
async def test_file_read_missing_file_returns_structured_error() -> None:
    tools = BuiltinTools()
    result = await tools.invoke(_call("file_read", {"path": "/no/such/file.abc"}))
    assert result.ok is False
    assert "not found" in result.error.lower() or "no such" in result.error.lower()


@pytest.mark.asyncio
async def test_file_read_missing_path_arg() -> None:
    tools = BuiltinTools()
    result = await tools.invoke(_call("file_read", {}))
    assert result.ok is False
    assert "path" in result.error


# ── file_write happy path ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_file_write_creates_file_and_reports_side_effect() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "nested" / "out.txt"
        tools = BuiltinTools(allowed_dirs=[tmp])
        result = await tools.invoke(_call("file_write", {
            "path": str(p), "content": "hello",
        }))
        assert result.ok is True
        assert p.exists()
        assert p.read_text(encoding="utf-8") == "hello"
        # side_effects carries the resolved path for the grader.
        assert len(result.side_effects) == 1
        assert str(p.resolve()) in result.side_effects[0]
        # content stays a structured dict so graders / bus consumers can
        # inspect it cleanly; agent_loop stringifies it for the LLM.
        assert result.content["bytes"] == 5
        assert result.content["path"] == str(p)


@pytest.mark.asyncio
async def test_file_write_overwrites_existing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "f.txt"
        p.write_text("old", encoding="utf-8")
        tools = BuiltinTools(allowed_dirs=[tmp])
        result = await tools.invoke(_call("file_write", {
            "path": str(p), "content": "new",
        }))
        assert result.ok is True
        assert p.read_text(encoding="utf-8") == "new"


@pytest.mark.asyncio
async def test_file_write_rejects_non_string_content() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tools = BuiltinTools(allowed_dirs=[tmp])
        result = await tools.invoke(_call("file_write", {
            "path": str(Path(tmp) / "x.txt"),
            "content": 12345,  # not a string
        }))
        assert result.ok is False
        assert "content" in result.error


# ── allowlist ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_allowlist_blocks_write_outside(tmp_path: Path) -> None:
    outside = tmp_path.parent / "_outside_xmc_test.txt"
    tools = BuiltinTools(allowed_dirs=[tmp_path])
    try:
        result = await tools.invoke(_call("file_write", {
            "path": str(outside), "content": "should not land",
        }))
        assert result.ok is False
        assert "permission" in result.error.lower()
        assert not outside.exists()
    finally:
        # Defensive cleanup in case the test framework's tmp_path.parent is
        # shared with other runs.
        if outside.exists():
            outside.unlink()


@pytest.mark.asyncio
async def test_allowlist_blocks_read_outside(tmp_path: Path) -> None:
    # Create a file in tmp_path/ but allowlist only a sibling dir.
    real = tmp_path / "real.txt"
    real.write_text("x", encoding="utf-8")
    other = tmp_path.parent / "_other"
    other.mkdir(exist_ok=True)
    try:
        tools = BuiltinTools(allowed_dirs=[other])
        result = await tools.invoke(_call("file_read", {"path": str(real)}))
        assert result.ok is False
        assert "permission" in result.error.lower()
    finally:
        if other.exists():
            other.rmdir()


@pytest.mark.asyncio
async def test_no_allowlist_means_trust_caller(tmp_path: Path) -> None:
    p = tmp_path / "ok.txt"
    p.write_text("yo", encoding="utf-8")
    tools = BuiltinTools()  # None allowlist
    result = await tools.invoke(_call("file_read", {"path": str(p)}))
    assert result.ok is True


# ── unknown tool ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unknown_tool_returns_structured_error() -> None:
    tools = BuiltinTools()
    result = await tools.invoke(_call("definitely_not_a_tool", {}))
    assert result.ok is False
    assert "unknown tool" in result.error


# ── list_dir ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_dir_lists_children(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "b.txt").write_text("bb", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    tools = BuiltinTools(allowed_dirs=[tmp_path])
    r = await tools.invoke(_call("list_dir", {"path": str(tmp_path)}))
    assert r.ok is True
    # Expect 3 entries, one per line.
    lines = r.content.splitlines()
    body = "\n".join(lines[1:])  # skip the "N entries" header
    assert "a.txt" in body and "b.txt" in body and "sub" in body
    # Directory prefixed with 'd', files with 'f'.
    assert any(line.startswith("d ") and "sub" in line for line in lines)
    assert any(line.startswith("f ") and "a.txt" in line for line in lines)


@pytest.mark.asyncio
async def test_list_dir_honors_pattern(tmp_path: Path) -> None:
    (tmp_path / "x.md").write_text("", encoding="utf-8")
    (tmp_path / "y.py").write_text("", encoding="utf-8")
    (tmp_path / "z.md").write_text("", encoding="utf-8")
    tools = BuiltinTools(allowed_dirs=[tmp_path])
    r = await tools.invoke(_call("list_dir", {
        "path": str(tmp_path), "pattern": "*.md",
    }))
    assert r.ok is True
    assert "x.md" in r.content and "z.md" in r.content
    assert "y.py" not in r.content


@pytest.mark.asyncio
async def test_list_dir_rejects_missing_dir(tmp_path: Path) -> None:
    tools = BuiltinTools()
    r = await tools.invoke(_call("list_dir", {
        "path": str(tmp_path / "does_not_exist"),
    }))
    assert r.ok is False
    assert "does not exist" in r.error


@pytest.mark.asyncio
async def test_list_dir_respects_allowlist(tmp_path: Path) -> None:
    outside = tmp_path.parent / "_ls_outside"
    outside.mkdir(exist_ok=True)
    try:
        tools = BuiltinTools(allowed_dirs=[tmp_path])
        r = await tools.invoke(_call("list_dir", {"path": str(outside)}))
        assert r.ok is False
        assert "permission" in r.error.lower()
    finally:
        if outside.exists():
            outside.rmdir()


# ── bash ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bash_runs_a_command() -> None:
    tools = BuiltinTools()
    # 'echo' works on both cmd and POSIX sh.
    r = await tools.invoke(_call("bash", {"command": "echo XMCLAW_OK"}))
    assert r.ok is True
    assert "XMCLAW_OK" in r.content
    assert "[exit 0]" in r.content


@pytest.mark.asyncio
async def test_bash_surfaces_nonzero_exit_without_raising() -> None:
    tools = BuiltinTools()
    r = await tools.invoke(_call("bash", {
        "command": "exit 7",
    }))
    assert r.ok is False
    assert "exit 7" in r.content or "7" in (r.error or "")


@pytest.mark.asyncio
async def test_bash_disabled_refuses() -> None:
    tools = BuiltinTools(enable_bash=False)
    r = await tools.invoke(_call("bash", {"command": "echo hi"}))
    assert r.ok is False
    assert "disabled" in r.error.lower()


@pytest.mark.asyncio
async def test_bash_rejects_missing_command() -> None:
    tools = BuiltinTools()
    r = await tools.invoke(_call("bash", {}))
    assert r.ok is False
    assert "command" in r.error.lower()


# ── web tools (mocked to avoid network flakes) ──────────────────────────

@pytest.mark.asyncio
async def test_web_fetch_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    class _FakeResp:
        status_code = 200
        reason_phrase = "OK"
        text = "<html>hello</html>"

    class _FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, url, headers=None):
            _FakeResp.url = url
            return _FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    tools = BuiltinTools()
    r = await tools.invoke(_call("web_fetch", {
        "url": "https://example.com/",
    }))
    assert r.ok is True
    assert "hello" in r.content
    assert "200 OK" in r.content


@pytest.mark.asyncio
async def test_web_fetch_rejects_non_http() -> None:
    tools = BuiltinTools()
    r = await tools.invoke(_call("web_fetch", {"url": "file:///etc/passwd"}))
    assert r.ok is False
    assert "http" in r.error.lower()


@pytest.mark.asyncio
async def test_web_fetch_disabled_refuses() -> None:
    tools = BuiltinTools(enable_web=False)
    r = await tools.invoke(_call("web_fetch", {"url": "https://example.com"}))
    assert r.ok is False
    assert "disabled" in r.error.lower()


@pytest.mark.asyncio
async def test_web_search_parses_results(monkeypatch: pytest.MonkeyPatch) -> None:
    """Feed a fake DDG HTML page and confirm we extract titles + urls."""
    import httpx

    html = '''
    <div>
      <a class="result__a" href="/l/?uddg=https%3A//example.com/first">First Result</a>
      <a class="result__snippet">This is the first snippet.</a>
      <a class="result__a" href="https://example.org/direct">Direct Link</a>
      <a class="result__snippet">Second snippet here.</a>
    </div>
    '''

    class _FakeResp:
        status_code = 200
        text = html

    class _FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def post(self, url, data=None, headers=None):
            return _FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    tools = BuiltinTools()
    r = await tools.invoke(_call("web_search", {
        "query": "anything", "max_results": 5,
    }))
    assert r.ok is True
    assert "First Result" in r.content
    assert "https://example.com/first" in r.content
    assert "Direct Link" in r.content


@pytest.mark.asyncio
async def test_web_search_disabled_refuses() -> None:
    tools = BuiltinTools(enable_web=False)
    r = await tools.invoke(_call("web_search", {"query": "x"}))
    assert r.ok is False
    assert "disabled" in r.error.lower()


# ── agent loop: tool-error content surface ──────────────────────────────

@pytest.mark.asyncio
async def test_failed_tool_content_is_error_string_not_None_str() -> None:
    """The regression the user hit: file_read on a non-allowed path
    failed, the agent_loop packaged content=str(None)='None' into the
    tool message, and the model hallucinated 'file is empty'.

    Fix: agent_loop must pass the structured ``result.error`` as the
    tool message content when ok=False. Verify by driving a whole
    run_turn with a scripted LLM that WILL see the tool-result message.
    """
    from xmclaw.core.bus import InProcessEventBus
    from xmclaw.core.ir import ToolCallShape
    from xmclaw.daemon.agent_loop import AgentLoop
    from xmclaw.providers.llm.base import (
        LLMResponse, Message, Pricing,
    )

    class _RecordingLLM:
        model = "rec"
        def __init__(self) -> None:
            self.seen: list[list[Message]] = []
            self._i = 0
            self.script = [
                LLMResponse(
                    content="",
                    tool_calls=(__import__("xmclaw.core.ir", fromlist=["ToolCall"]).ToolCall(
                        name="file_read",
                        args={"path": "/definitely/not/allowed.txt"},
                        provenance="anthropic", id="t1",
                    ),),
                ),
                LLMResponse(content="final"),
            ]

        async def stream(self, *a, **k):
            if False:
                yield None

        async def complete(self, messages, tools=None):
            self.seen.append(list(messages))
            r = self.script[self._i]
            self._i += 1
            return r

        @property
        def tool_call_shape(self) -> ToolCallShape:
            return ToolCallShape.ANTHROPIC_NATIVE

        @property
        def pricing(self) -> Pricing:
            return Pricing()

    # Allowlist excludes /definitely/not, so file_read will fail with
    # PermissionError -> ToolResult(ok=False, error="permission denied...").
    tools = BuiltinTools(allowed_dirs=["/some/safe/place"])
    llm = _RecordingLLM()
    agent = AgentLoop(llm=llm, bus=InProcessEventBus(), tools=tools)
    await agent.run_turn("s1", "please read that file")

    # The LLM's second call must see the tool message with ACTUAL error
    # text in content -- not "None", not empty string.
    second_call = llm.seen[1]
    tool_msgs = [m for m in second_call if m.role == "tool"]
    assert len(tool_msgs) == 1
    body = tool_msgs[0].content or ""
    assert body.startswith("ERROR:"), (
        f"tool error didn't surface as ERROR: prefix, got {body!r}"
    )
    assert "permission" in body.lower(), (
        f"real error message lost, got {body!r}"
    )
    # And critically: no longer the string "None".
    assert body != "None"
