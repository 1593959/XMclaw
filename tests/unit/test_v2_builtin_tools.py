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
    """Every spec has an object parameters_schema. Several tools take
    no required args:

    * ``todo_read``           — no-arg fetch
    * ``agent_status``        — B-49 self-introspection
    * ``memory_compact``      — B-52 dream trigger
    * ``enter_worktree``      — B-94 (auto-generates name + base if omitted)
    * ``exit_worktree``       — B-94 (``keep`` is optional flag)
    * ``journal_recall``      — Epic #24 Phase 2.5 (defaults limit=5,
      days_back=30; the agent typically calls without args to scan
      "what have I been doing recently")
    """
    zero_arg = {
        "todo_read", "agent_status", "memory_compact",
        "enter_worktree", "exit_worktree",
        "journal_recall",
        "recall_user_preferences",  # Epic #24 Phase 4.2
    }
    for spec in BuiltinTools().list_tools():
        assert spec.parameters_schema["type"] == "object"
        if spec.name in zero_arg:
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


# ── apply_patch ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_apply_patch_single_edit_succeeds(tmp_path: Path) -> None:
    p = tmp_path / "f.py"
    p.write_text("hello world\nbye\n", encoding="utf-8")
    tools = BuiltinTools(allowed_dirs=[tmp_path])
    r = await tools.invoke(_call("apply_patch", {
        "path": str(p),
        "edits": [{"old_text": "hello world", "new_text": "HELLO"}],
    }))
    assert r.ok is True, r.error
    assert p.read_text(encoding="utf-8") == "HELLO\nbye\n"
    assert r.content["edits_applied"] == 1
    assert r.content["bytes_before"] == 16
    assert r.content["bytes_after"] == 10
    assert r.content["delta"] == -6
    assert r.side_effects == (str(p.resolve()),)


@pytest.mark.asyncio
async def test_apply_patch_sequential_edits(tmp_path: Path) -> None:
    p = tmp_path / "g.txt"
    p.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    tools = BuiltinTools(allowed_dirs=[tmp_path])
    r = await tools.invoke(_call("apply_patch", {
        "path": str(p),
        "edits": [
            {"old_text": "alpha", "new_text": "A"},
            {"old_text": "gamma", "new_text": "G"},
        ],
    }))
    assert r.ok is True, r.error
    assert p.read_text(encoding="utf-8") == "A\nbeta\nG\n"
    assert r.content["edits_applied"] == 2


@pytest.mark.asyncio
async def test_apply_patch_listed_in_default_roster() -> None:
    names = {t.name for t in BuiltinTools().list_tools()}
    assert "apply_patch" in names


@pytest.mark.asyncio
async def test_apply_patch_missing_path_arg() -> None:
    r = await BuiltinTools().invoke(_call("apply_patch", {
        "edits": [{"old_text": "a", "new_text": "b"}],
    }))
    assert r.ok is False
    assert "path" in r.error.lower()


@pytest.mark.asyncio
async def test_apply_patch_empty_edits_rejected(tmp_path: Path) -> None:
    p = tmp_path / "h.txt"
    p.write_text("x", encoding="utf-8")
    tools = BuiltinTools(allowed_dirs=[tmp_path])
    r = await tools.invoke(_call("apply_patch", {
        "path": str(p), "edits": [],
    }))
    assert r.ok is False
    assert "edits" in r.error.lower()


@pytest.mark.asyncio
async def test_apply_patch_edits_must_be_list(tmp_path: Path) -> None:
    p = tmp_path / "h.txt"
    p.write_text("x", encoding="utf-8")
    tools = BuiltinTools(allowed_dirs=[tmp_path])
    r = await tools.invoke(_call("apply_patch", {
        "path": str(p), "edits": "not-a-list",
    }))
    assert r.ok is False
    assert "edits" in r.error.lower()


@pytest.mark.asyncio
async def test_apply_patch_edit_must_be_object(tmp_path: Path) -> None:
    p = tmp_path / "h.txt"
    p.write_text("x", encoding="utf-8")
    tools = BuiltinTools(allowed_dirs=[tmp_path])
    r = await tools.invoke(_call("apply_patch", {
        "path": str(p), "edits": ["x"],
    }))
    assert r.ok is False
    assert "edits[0]" in r.error


@pytest.mark.asyncio
async def test_apply_patch_rejects_empty_old_text(tmp_path: Path) -> None:
    p = tmp_path / "h.txt"
    p.write_text("x", encoding="utf-8")
    tools = BuiltinTools(allowed_dirs=[tmp_path])
    r = await tools.invoke(_call("apply_patch", {
        "path": str(p),
        "edits": [{"old_text": "", "new_text": "y"}],
    }))
    assert r.ok is False
    assert "old_text" in r.error


@pytest.mark.asyncio
async def test_apply_patch_rejects_non_string_new_text(tmp_path: Path) -> None:
    p = tmp_path / "h.txt"
    p.write_text("x", encoding="utf-8")
    tools = BuiltinTools(allowed_dirs=[tmp_path])
    r = await tools.invoke(_call("apply_patch", {
        "path": str(p),
        "edits": [{"old_text": "x", "new_text": 12}],
    }))
    assert r.ok is False
    assert "new_text" in r.error


@pytest.mark.asyncio
async def test_apply_patch_missing_file(tmp_path: Path) -> None:
    tools = BuiltinTools(allowed_dirs=[tmp_path])
    r = await tools.invoke(_call("apply_patch", {
        "path": str(tmp_path / "ghost.txt"),
        "edits": [{"old_text": "a", "new_text": "b"}],
    }))
    assert r.ok is False
    assert "does not exist" in r.error


@pytest.mark.asyncio
async def test_apply_patch_old_text_not_found(tmp_path: Path) -> None:
    p = tmp_path / "h.txt"
    p.write_text("alpha beta\n", encoding="utf-8")
    tools = BuiltinTools(allowed_dirs=[tmp_path])
    r = await tools.invoke(_call("apply_patch", {
        "path": str(p),
        "edits": [{"old_text": "MISSING", "new_text": "X"}],
    }))
    assert r.ok is False
    assert "not found" in r.error
    # File untouched.
    assert p.read_text(encoding="utf-8") == "alpha beta\n"


@pytest.mark.asyncio
async def test_apply_patch_old_text_multiple_matches_aborts(tmp_path: Path) -> None:
    p = tmp_path / "h.txt"
    p.write_text("foo\nfoo\nfoo\n", encoding="utf-8")
    tools = BuiltinTools(allowed_dirs=[tmp_path])
    r = await tools.invoke(_call("apply_patch", {
        "path": str(p),
        "edits": [{"old_text": "foo", "new_text": "bar"}],
    }))
    assert r.ok is False
    assert "3 times" in r.error
    # Original preserved — atomicity guarantee.
    assert p.read_text(encoding="utf-8") == "foo\nfoo\nfoo\n"


@pytest.mark.asyncio
async def test_apply_patch_noop_rejected(tmp_path: Path) -> None:
    p = tmp_path / "h.txt"
    p.write_text("same\n", encoding="utf-8")
    tools = BuiltinTools(allowed_dirs=[tmp_path])
    r = await tools.invoke(_call("apply_patch", {
        "path": str(p),
        "edits": [{"old_text": "same", "new_text": "same"}],
    }))
    assert r.ok is False
    assert "no change" in r.error.lower()


@pytest.mark.asyncio
async def test_apply_patch_does_not_leave_temp_file(tmp_path: Path) -> None:
    p = tmp_path / "h.py"
    p.write_text("x = 1\n", encoding="utf-8")
    tools = BuiltinTools(allowed_dirs=[tmp_path])
    r = await tools.invoke(_call("apply_patch", {
        "path": str(p),
        "edits": [{"old_text": "x = 1", "new_text": "x = 2"}],
    }))
    assert r.ok is True
    # The atomic-write tmp suffix must be gone after replace().
    assert not (tmp_path / "h.py.patch.tmp").exists()
    assert p.read_text(encoding="utf-8") == "x = 2\n"


@pytest.mark.asyncio
async def test_apply_patch_respects_allowlist(tmp_path: Path) -> None:
    outside = tmp_path.parent / "_apply_patch_outside.txt"
    outside.write_text("hello", encoding="utf-8")
    try:
        tools = BuiltinTools(allowed_dirs=[tmp_path])
        r = await tools.invoke(_call("apply_patch", {
            "path": str(outside),
            "edits": [{"old_text": "hello", "new_text": "BYE"}],
        }))
        assert r.ok is False
        assert "permission" in r.error.lower()
        # File untouched.
        assert outside.read_text(encoding="utf-8") == "hello"
    finally:
        if outside.exists():
            outside.unlink()


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

        async def complete_streaming(
            self, messages, tools=None, *,
            on_chunk=None, on_thinking_chunk=None, cancel=None,
        ):
            # B-39 / B-91: AgentLoop passes ``cancel=cancel_event`` for
            # mid-stream interruption AND ``on_thinking_chunk`` for
            # reasoning deltas. Mocks accept-and-ignore both.
            r = await self.complete(messages, tools=tools)
            if on_chunk is not None and r.content:
                await on_chunk(r.content)
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


# ── B-203 sqlite_query schema-hint on error ──────────────────────────────


@pytest.mark.asyncio
async def test_sqlite_query_no_such_table_lists_available_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """probe_b200_v2 audit_pref_kinds turn: 6/11 sqlite_query calls
    failed with "no such table: memories" because the agent guessed.
    Tool must surface the real schema in the error so the next hop
    can recover without a second tool call."""
    import sqlite3

    # Build a fake events.db with a couple of real tables, then
    # redirect data_dir() so the tool sees it.
    fake_root = tmp_path / "fake_home"
    (fake_root / "v2").mkdir(parents=True)
    db_path = fake_root / "v2" / "events.db"
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE events (id TEXT, ts REAL, type TEXT)")
    con.execute("CREATE TABLE sessions (id TEXT, started REAL)")
    con.commit()
    con.close()

    monkeypatch.setattr(
        "xmclaw.utils.paths.data_dir",
        lambda: fake_root,
    )

    tools = BuiltinTools()
    call = _call("sqlite_query", {
        "db": "events", "sql": "SELECT * FROM memories",
    })
    result = await tools.invoke(call)

    assert result.ok is False
    assert "no such table" in (result.error or "")
    # B-203: schema hint must enumerate real tables.
    assert "events" in (result.error or "")
    assert "sessions" in (result.error or "")
    assert "available tables" in (result.error or "")


@pytest.mark.asyncio
async def test_sqlite_query_no_such_column_lists_columns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the table exists but the column doesn't, surface the
    real columns of that table — same recovery affordance, one
    level deeper."""
    import sqlite3

    fake_root = tmp_path / "fake_home"
    (fake_root / "v2").mkdir(parents=True)
    db_path = fake_root / "v2" / "memory.db"
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE memory_items "
        "(id TEXT, text TEXT, kind TEXT, evidence_count INTEGER)"
    )
    con.commit()
    con.close()

    monkeypatch.setattr(
        "xmclaw.utils.paths.data_dir",
        lambda: fake_root,
    )

    tools = BuiltinTools()
    call = _call("sqlite_query", {
        "db": "memory",
        "sql": "SELECT bogus_col FROM memory_items",
    })
    result = await tools.invoke(call)

    assert result.ok is False
    assert "no such column" in (result.error or "")
    # Schema-hint enumerates real columns of memory_items.
    err = result.error or ""
    assert "memory_items" in err
    assert "evidence_count" in err
    assert "kind" in err


@pytest.mark.asyncio
async def test_sqlite_query_unrelated_error_no_schema_noise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hint must ONLY appear for schema-shape errors; a syntax error
    should not get a tables list dump."""
    import sqlite3

    fake_root = tmp_path / "fake_home"
    (fake_root / "v2").mkdir(parents=True)
    db_path = fake_root / "v2" / "events.db"
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE events (id TEXT)")
    con.commit()
    con.close()

    monkeypatch.setattr(
        "xmclaw.utils.paths.data_dir",
        lambda: fake_root,
    )

    tools = BuiltinTools()
    # Genuine syntax error — incomplete WHERE clause. Not "no such
    # table" / "no such column", so the schema-hint must NOT fire.
    call = _call("sqlite_query", {
        "db": "events", "sql": "SELECT * FROM events WHERE",
    })
    result = await tools.invoke(call)

    assert result.ok is False
    err = result.error or ""
    assert "syntax error" in err.lower() or "incomplete" in err.lower()
    assert "available tables" not in err
    assert "columns of" not in err
