"""BuiltinTools (file_read / file_write) — unit tests.

Covers: happy paths, structured-error paths, side_effects population,
allowlist enforcement. Every failure mode must return a structured
ToolResult with ``ok=False`` and an ``error`` message — the orchestrator
inspects those, never swallowed exceptions.
"""
from __future__ import annotations

import tempfile
import subprocess
from pathlib import Path

import pytest

from xmclaw.core.ir import ToolCall
from xmclaw.memory.v2.candidates import MemoryCandidateStore
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
    memory_spec = next(t for t in tools if t.name == "memory")
    actions = memory_spec.parameters_schema["properties"]["action"]["enum"]
    assert "environment" not in actions
    assert "memory_decision" in names


@pytest.mark.asyncio
async def test_memory_decision_skip_requires_reason() -> None:
    tools = BuiltinTools()
    result = await tools.invoke(_call("memory_decision", {
        "action": "skip",
        "reason": "当前只是问候",
    }))
    assert result.ok is False
    assert "skipped_reason" in result.error


@pytest.mark.asyncio
async def test_memory_decision_can_create_candidate(tmp_path: Path) -> None:
    class Gateway:
        candidate_store = MemoryCandidateStore(tmp_path / "candidates.db")

    tools = BuiltinTools()
    tools.set_memory_gateway(Gateway())
    result = await tools.invoke(_call("memory_decision", {
        "action": "write_candidate",
        "reason": "用户明确给出长期偏好",
        "candidate_text": "用户偏好使用中文简洁回复。",
        "kind": "preference",
        "scope": "user",
        "bucket": "user_preference",
        "confidence": 0.9,
    }))
    assert result.ok is True
    assert result.content["candidate_created"] is True
    assert result.content["candidate"]["quality_score"] > 0.5


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


@pytest.mark.asyncio
async def test_bash_shell_execution_policy_disabled_refuses() -> None:
    tools = BuiltinTools(shell_execution_policy="disabled")
    result = await tools.invoke(_call("bash", {"command": "echo hi"}))

    assert result.ok is False
    assert "execution_policy=disabled" in result.error


@pytest.mark.asyncio
async def test_bash_shell_execution_policy_docker_uses_sandbox(tmp_path: Path) -> None:
    calls: list[dict] = []

    def runner(args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return subprocess.CompletedProcess(args, 0, b"docker ok\n", b"")

    tools = BuiltinTools(
        shell_execution_policy="docker",
        shell_sandbox_image="alpine:3.20",
        shell_sandbox_memory="768m",
        shell_sandbox_cpus="0.5",
        shell_sandbox_pids_limit=128,
        shell_sandbox_network="bridge",
        shell_sandbox_runner=runner,
    )
    result = await tools.invoke(
        _call("bash", {"command": "echo hi", "cwd": str(tmp_path)}),
    )

    assert result.ok is True
    assert "docker ok" in result.content
    assert calls
    args = calls[0]["args"]
    assert args[:3] == ["docker", "run", "--rm"]
    assert "--network" in args and "bridge" in args
    assert "--memory" in args and "768m" in args
    assert "--cpus" in args and "0.5" in args
    assert "--pids-limit" in args and "128" in args
    assert "--cap-drop" in args and "ALL" in args
    assert "alpine:3.20" in args
    assert f"{tmp_path.resolve()}:/workspace" in args


def test_bash_shell_execution_policy_rejects_unknown_name() -> None:
    with pytest.raises(ValueError):
        BuiltinTools(shell_execution_policy="mystery")


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
        "enter_plan_mode",  # Wave-32+ — no-arg flag flip
        "journal_recall",
        "recall_user_preferences",  # Epic #24 Phase 4.2
        # 2026-05-26: memory_dedup defaults to dry_run + every-scope —
        # the agent calls it with zero args to preview "anything I
        # could clean up?". Required fields would force premature
        # commitment to a scope.
        "memory_dedup",
        # 2026-05-28: memory_inspect is read-only health probe —
        # zero-arg "show me the whole picture" usage is the primary
        # mode; restricting by scope is optional.
        "memory_inspect",
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
async def test_file_read_persona_name_outside_persona_dir_redirects() -> None:
    """2026-05-18 Wave-31 fix: when ``file_read`` fails on a path
    whose basename matches a persona file (AGENTS / MEMORY / USER /
    TOOLS / SOUL / LEARNING / IDENTITY / BOOTSTRAP), the error
    message points at ``update_persona`` /
    ``recall_user_preferences`` and the canonical
    ``~/.xmclaw/persona/profiles/`` location. Without this, real-data
    Kimi K2.6 turns kept retrying ``Desktop\\AGENTS.md`` →
    ``Desktop\\XMclaw\\AGENTS.md`` → various other dead paths
    despite the system prompt's path note (it sits 20K chars deep
    and the model's attention slips past it)."""
    tools = BuiltinTools()
    result = await tools.invoke(_call("file_read", {"path": "/tmp/AGENTS.md"}))
    assert result.ok is False
    assert "persona files" in result.error
    assert "~/.xmclaw/persona/profiles/" in result.error
    assert "recall_user_preferences" in result.error


@pytest.mark.asyncio
async def test_file_read_persona_path_inside_persona_dir_no_redirect() -> None:
    """The redirect must NOT fire for paths already pointing at the
    canonical ``~/.xmclaw/persona/profiles/`` location — that's a
    legitimate file-missing case (fresh install, profile not built
    yet) and the agent should see the plain error, not a redirect
    loop back to itself."""
    tools = BuiltinTools()
    result = await tools.invoke(_call("file_read", {
        "path": "/home/u/.xmclaw/persona/profiles/default/AGENTS.md",
    }))
    assert result.ok is False
    # Plain not-found, no redirect chrome.
    assert "persona files" not in result.error


@pytest.mark.asyncio
async def test_file_write_persona_name_outside_persona_dir_blocked() -> None:
    """``file_write`` is hard-blocked when the path is a persona-name
    in the wrong directory. Without this the agent "creates" e.g.
    ``Desktop\\LEARNING.md`` with real lesson text — but the daemon
    reads from the canonical persona path so the write has no
    effect on the next turn AND the user is left with a confusing
    stray file on disk."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        tools = BuiltinTools(allowed_dirs=[tmp])
        target = str(Path(tmp) / "LEARNING.md")
        result = await tools.invoke(_call(
            "file_write", {"path": target, "content": "lessons..."},
        ))
        assert result.ok is False
        assert "update_persona" in result.error
        assert "LEARNING.md" in result.error


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


@pytest.mark.asyncio
async def test_file_write_missing_content_creates_empty_file() -> None:
    """Wave 25.5: omitting ``content`` (or passing null) is treated as
    scaffolding an empty file rather than a hard error. Common case:
    agent wants to create a placeholder before writing into it via
    apply_patch / a subsequent file_write."""
    with tempfile.TemporaryDirectory() as tmp:
        tools = BuiltinTools(allowed_dirs=[tmp])
        target = Path(tmp) / "scaffold.py"
        # No `content` key at all.
        r = await tools.invoke(_call("file_write", {
            "path": str(target),
        }))
        assert r.ok is True, r.error
        assert target.exists()
        assert target.read_text(encoding="utf-8") == ""


@pytest.mark.asyncio
async def test_file_write_null_content_creates_empty_file() -> None:
    """Same tolerance for explicit ``null`` — some JSON-mode LLMs emit
    null instead of omitting the key."""
    with tempfile.TemporaryDirectory() as tmp:
        tools = BuiltinTools(allowed_dirs=[tmp])
        target = Path(tmp) / "scaffold.py"
        r = await tools.invoke(_call("file_write", {
            "path": str(target),
            "content": None,
        }))
        assert r.ok is True
        assert target.read_text(encoding="utf-8") == ""


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
        # Wave-27 fix-LAT8: web_fetch now inspects content-type to
        # choose between text-decode (default) and image-save paths.
        # A plain text/html content-type keeps the legacy text path.
        headers: dict = {"content-type": "text/html; charset=utf-8"}
        content = b"<html>hello</html>"

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
async def test_web_fetch_sends_real_chrome_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-05-28: public sites 403'd the old XMclaw/2.x UA. Verify
    we now ship a Chrome UA + Accept-* + Sec-* headers — the
    minimum a modern browser sends so anti-bot middleboxes don't
    bounce us on day one."""
    import httpx

    captured: dict = {}

    class _Resp:
        status_code = 200
        reason_phrase = "OK"
        text = "ok"
        headers = {"content-type": "text/html"}
        content = b"ok"

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, url, headers=None):
            captured["headers"] = headers or {}
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    tools = BuiltinTools()
    await tools.invoke(_call("web_fetch", {"url": "https://example.com/"}))

    h = captured["headers"]
    assert "Chrome/" in h["User-Agent"]
    assert "Mozilla/5.0" in h["User-Agent"]
    assert "Accept" in h
    assert "Accept-Language" in h
    assert "Accept-Encoding" in h
    assert h.get("Sec-Fetch-Mode") == "navigate"


@pytest.mark.asyncio
async def test_web_fetch_retries_on_transient_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transient ConnectError → retry → success. Without retry the
    agent would see the failure and burn context on identical
    re-fetches."""
    import httpx

    class _Resp:
        status_code = 200
        reason_phrase = "OK"
        text = "ok"
        headers = {"content-type": "text/html"}
        content = b"ok"

    attempts = {"n": 0}

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, url, headers=None):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise httpx.ConnectError("flaky")
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    tools = BuiltinTools()
    r = await tools.invoke(_call("web_fetch", {"url": "https://example.com/"}))
    assert r.ok is True
    assert attempts["n"] == 2


@pytest.mark.asyncio
async def test_web_fetch_exhausts_retries_returns_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All 3 attempts fail → error must (a) include the exception
    class name, (b) report attempts count, (c) point at browser_open
    as the fallback so the agent doesn't keep hammering."""
    import httpx

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, url, headers=None):
            raise httpx.ConnectError("")  # empty message — the B-233 trap

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    tools = BuiltinTools()
    r = await tools.invoke(_call("web_fetch", {"url": "https://example.com/"}))
    assert r.ok is False
    assert "3 attempts" in r.error
    assert "ConnectError" in r.error
    assert "browser_open" in r.error


@pytest.mark.asyncio
async def test_web_fetch_403_with_cloudflare_body_hints_browser_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 403 from a Cloudflare-protected page now appends the
    browser_open hint so the agent knows the next step."""
    import httpx

    class _Resp:
        status_code = 403
        reason_phrase = "Forbidden"
        text = "<html>Just a moment... Cloudflare verification</html>"
        headers = {"content-type": "text/html"}
        content = b""

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def get(self, url, headers=None):
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    tools = BuiltinTools()
    # 2026-05-28: a bot-blocked 403 first tries the headless-Chromium fallback
    # (_fetch_via_browser). Stub it to FAIL so we exercise the "both raw HTTP
    # and browser fallback failed → return the browser_open hint" path. Without
    # this the test launches a real browser and fetches example.com for real.
    async def _no_browser(url, max_chars):  # noqa: ANN001, ARG001
        return ("", 403, "no browser available in test")
    tools._fetch_via_browser = _no_browser  # type: ignore[attr-defined]
    r = await tools.invoke(_call("web_fetch", {"url": "https://example.com/"}))
    assert r.ok is False
    assert "403" in r.error
    assert "bot-blocked" in r.error.lower()
    assert "browser_open" in r.error


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
async def test_web_fetch_blocks_private_ips() -> None:
    """SSRF protection: raw private / loopback IPs are refused
    before any HTTP request is made."""
    tools = BuiltinTools()
    blocked = [
        "http://127.0.0.1/secret",
        "http://127.0.0.1:8080/admin",
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://0.0.0.0/",
        "http://[::1]/",
        "http://[fe80::1]/",
        "http://localhost/",
        "http://metadata.google.internal/",
    ]
    for url in blocked:
        r = await tools.invoke(_call("web_fetch", {"url": url}))
        assert r.ok is False, f"expected {url} to be blocked"
        assert "ssrf" in r.error.lower() or "private" in r.error.lower() or "disallowed" in r.error.lower(), (
            f"expected SSRF error for {url}, got: {r.error}"
        )


@pytest.mark.asyncio
async def test_web_fetch_blocks_credential_urls() -> None:
    """SSRF protection: URLs containing @ (credentials / redirect trick)
    are refused."""
    tools = BuiltinTools()
    r = await tools.invoke(_call("web_fetch", {"url": "http://evil.com@127.0.0.1/"}))
    assert r.ok is False
    assert "ssrf" in r.error.lower() or "credential" in r.error.lower()


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


@pytest.mark.asyncio
async def test_web_search_falls_back_to_bing_when_ddg_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-05-28: from CN networks DDG is often blocked → tool would
    hang or 0-result. Now: DDG times out / errors → auto-fallback to
    Bing CN HTML, agent gets real results, agent never has to know."""
    import httpx

    bing_html = '''
    <ol id="b_results">
      <li class="b_algo">
        <h2><a href="https://example.com/monaco">Monaco Editor CDN</a></h2>
        <div class="b_caption"><p>Monaco editor v0.45 on jsdelivr.</p></div>
      </li>
      <li class="b_algo">
        <h2><a href="https://unpkg.com/monaco-editor/">unpkg link</a></h2>
        <p class="b_lineclamp2">Latest monaco-editor on unpkg.</p>
      </li>
    </ol>
    '''

    class _BingResp:
        status_code = 200
        text = bing_html

    class _FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None

        async def post(self, url, data=None, headers=None):
            # DDG path always called via POST in our impl.
            raise httpx.ConnectError("connect timeout (simulated CN block)")

        async def get(self, url, headers=None, params=None):
            # Bing CN HTML scrape uses GET — return our fake SERP.
            assert "cn.bing.com" in url
            return _BingResp()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    tools = BuiltinTools()
    r = await tools.invoke(_call("web_search", {
        "query": "monaco-editor CDN", "max_results": 5,
    }))
    assert r.ok is True
    assert "Monaco Editor CDN" in r.content
    assert "unpkg" in r.content
    # The result note should say bing_cn won + primary ddg failed.
    assert "bing_cn" in r.content
    assert "ddg" in r.content.lower()


@pytest.mark.asyncio
async def test_web_search_all_engines_fail_gives_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When both DDG and Bing CN are unreachable, surface the last
    error + which engines were tried + a config-hint."""
    import httpx

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def post(self, url, data=None, headers=None):
            raise httpx.ConnectError("no route to host")
        async def get(self, url, headers=None, params=None):
            raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    tools = BuiltinTools()
    r = await tools.invoke(_call("web_search", {"query": "x"}))
    assert r.ok is False
    assert "ddg" in r.error and "bing_cn" in r.error
    assert "bing_api_key" in r.error  # config-hint surfaces


@pytest.mark.asyncio
async def test_web_search_disable_fallback_respected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If user sets evolution.search.disable_fallback=True, DDG
    failure stays terminal — no auto-Bing."""
    import httpx

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def post(self, url, data=None, headers=None):
            raise httpx.ConnectError("ddg down")
        async def get(self, url, headers=None, params=None):
            raise AssertionError(
                "fallback to Bing CN should NOT happen when disabled"
            )

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    tools = BuiltinTools(
        search_config_getter=lambda: {
            "provider": "ddg", "disable_fallback": True,
        },
    )
    r = await tools.invoke(_call("web_search", {"query": "x"}))
    assert r.ok is False
    assert "ddg" in r.error
    assert "bing_cn" not in r.error  # never tried


def test_parse_bing_html_extracts_typical_serp():
    """Cover both b_caption-wrapped and b_lineclamp snippet shapes."""
    from xmclaw.providers.tool._helpers import _parse_bing_html
    html = '''
    <ol id="b_results">
      <li class="b_algo">
        <h2><a href="https://a.example/path">Title A</a></h2>
        <div class="b_caption"><p>Snippet A here.</p></div>
      </li>
      <li class="b_algo">
        <h2><a href="https://b.example/">Title B</a></h2>
        <p class="b_lineclamp2">Snippet B here.</p>
      </li>
      <li class="b_algo">
        <h2><a href="https://c.example/">Title C</a></h2>
      </li>
    </ol>
    '''
    out = _parse_bing_html(html, max_results=5)
    assert len(out) == 3
    assert out[0] == {
        "title": "Title A",
        "url": "https://a.example/path",
        "snippet": "Snippet A here.",
    }
    assert out[1]["snippet"] == "Snippet B here."
    # Third entry has no snippet — empty string is fine, not None.
    assert out[2]["snippet"] == ""


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


# ── apply_patch reliability: whitespace-tolerant + replace_all (2026-06-15) ──

@pytest.mark.asyncio
async def test_apply_patch_tolerates_trailing_whitespace_drift(tmp_path: Path) -> None:
    """The #1 cause of edit-retry loops: the LLM's old_text lacks the
    file's trailing whitespace. Exact match fails → whitespace-tolerant
    fallback re-anchors on the real lines and applies."""
    p = tmp_path / "f.py"
    p.write_text("def f():\n    x = 1   \n    return x\n", encoding="utf-8")
    tools = BuiltinTools(allowed_dirs=[tmp_path])
    r = await tools.invoke(_call("apply_patch", {
        "path": str(p),
        # old_text has NO trailing spaces; file line "    x = 1   " has them.
        "edits": [{"old_text": "    x = 1\n    return x", "new_text": "    x = 2\n    return x"}],
    }))
    assert r.ok is True, r.error
    assert p.read_text(encoding="utf-8") == "def f():\n    x = 2\n    return x\n"


@pytest.mark.asyncio
async def test_apply_patch_tolerates_crlf_drift(tmp_path: Path) -> None:
    """LLM gives LF old_text; file is CRLF. Fallback still applies."""
    p = tmp_path / "g.txt"
    p.write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")
    tools = BuiltinTools(allowed_dirs=[tmp_path])
    r = await tools.invoke(_call("apply_patch", {
        "path": str(p),
        "edits": [{"old_text": "beta", "new_text": "BETA"}],
    }))
    assert r.ok is True, r.error
    assert "BETA" in p.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_apply_patch_ambiguous_fuzzy_match_aborts(tmp_path: Path) -> None:
    """When exact fails AND the whitespace-tolerant match hits multiple
    blocks, abort (don't guess) — unless replace_all is set."""
    p = tmp_path / "h.txt"
    p.write_text("x = 1 \nx = 1\n", encoding="utf-8")  # both normalise to "x = 1"
    tools = BuiltinTools(allowed_dirs=[tmp_path])
    r = await tools.invoke(_call("apply_patch", {
        "path": str(p),
        "edits": [{"old_text": "x = 1\n", "new_text": "y\n"}],
    }))
    # "x = 1\n" exact-matches the 2nd line once → count==1, applies. Use a
    # form that exact-misses both lines (trailing space variant) to force
    # the fuzzy path:
    p.write_text("x = 1 \nx = 1 \n", encoding="utf-8")
    r = await tools.invoke(_call("apply_patch", {
        "path": str(p),
        "edits": [{"old_text": "x = 1", "new_text": "y"}],
    }))
    # exact "x = 1" appears 0 times (both have trailing space) → fuzzy → 2 blocks → abort
    assert r.ok is False
    assert "ambiguous" in r.error.lower()


@pytest.mark.asyncio
async def test_apply_patch_replace_all(tmp_path: Path) -> None:
    p = tmp_path / "i.txt"
    p.write_text("foo\nfoo\nbar\nfoo\n", encoding="utf-8")
    tools = BuiltinTools(allowed_dirs=[tmp_path])
    r = await tools.invoke(_call("apply_patch", {
        "path": str(p),
        "edits": [{"old_text": "foo", "new_text": "baz", "replace_all": True}],
    }))
    assert r.ok is True, r.error
    assert p.read_text(encoding="utf-8") == "baz\nbaz\nbar\nbaz\n"


@pytest.mark.asyncio
async def test_apply_patch_multiple_occurrences_without_replace_all_aborts(tmp_path: Path) -> None:
    p = tmp_path / "j.txt"
    p.write_text("foo\nfoo\n", encoding="utf-8")
    tools = BuiltinTools(allowed_dirs=[tmp_path])
    r = await tools.invoke(_call("apply_patch", {
        "path": str(p),
        "edits": [{"old_text": "foo", "new_text": "baz"}],
    }))
    assert r.ok is False
    assert "replace_all" in r.error


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
            on_chunk=None, on_thinking_chunk=None,
            on_tool_block=None, on_stream_fallback=None, cancel=None,
            extended_thinking=None, **_kw,
        ):
            # B-39 / B-91 / Wave-32+: AgentLoop passes ``cancel``,
            # ``on_thinking_chunk``, and ``on_tool_block``. Mock
            # accepts-and-ignores all three.
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
    # B-205: events.db queries should NOT get the memory_search
    # redirect (that's only relevant for memory.db).
    assert "memory_search" not in (result.error or "")


@pytest.mark.asyncio
async def test_sqlite_query_memory_db_redirects_to_memory_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B-205 cross-tie: when an unknown-table error fires on
    memory.db (vs events.db), surface the memory_search redirect
    in addition to the table list. Most "no such table: memories"
    style errors come from agents trying to do semantic recall via
    raw SQL — point them at the right tool right inside the error."""
    import sqlite3

    fake_root = tmp_path / "fake_home"
    (fake_root / "v2").mkdir(parents=True)
    db_path = fake_root / "v2" / "memory.db"
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE memory_items (id TEXT, text TEXT)")
    con.commit()
    con.close()

    monkeypatch.setattr(
        "xmclaw.utils.paths.data_dir",
        lambda: fake_root,
    )

    tools = BuiltinTools()
    call = _call("sqlite_query", {
        "db": "memory", "sql": "SELECT * FROM memories",
    })
    result = await tools.invoke(call)

    assert result.ok is False
    err = result.error or ""
    # Both signals present: table list + redirect.
    assert "memory_items" in err
    assert "memory_search" in err
    assert "kind" in err  # the redirect mentions kind filter


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


# ── read_conversation_history ───────────────────────────────────────────

def test_read_conversation_history_not_advertised_without_store() -> None:
    """B-ContextLoss-3: tool is hidden when no session_store is wired."""
    tools = BuiltinTools()
    names = {t.name for t in tools.list_tools()}
    assert "read_conversation_history" not in names


def test_read_conversation_history_advertised_with_store() -> None:
    """B-ContextLoss-3: tool surfaces when session_store is wired."""
    from xmclaw.daemon.session_store import SessionStore
    store = SessionStore.__new__(SessionStore)
    tools = BuiltinTools(session_store=store)
    names = {t.name for t in tools.list_tools()}
    assert "read_conversation_history" in names


@pytest.mark.asyncio
async def test_read_conversation_history_returns_entries(
    tmp_path: Path,
) -> None:
    """B-ContextLoss-3: chronological browse returns formatted entries."""
    from xmclaw.daemon.session_store import SessionStore
    db = tmp_path / "sess.db"
    store = SessionStore(db)
    from xmclaw.providers.llm.base import Message
    store.save("sess-abc", [
        Message(role="user", content="hello"),
        Message(role="assistant", content="world"),
        Message(role="user", content="how are you"),
    ])

    tools = BuiltinTools(session_store=store)
    call = ToolCall(
        name="read_conversation_history",
        args={"limit": 2, "direction": "newest"},
        provenance="synthetic", session_id="sess-abc",
    )
    result = await tools.invoke(call)
    assert result.ok is True
    data = result.content
    assert isinstance(data, dict)
    assert data["total_messages"] == 3
    assert data["returned"] == 2
    assert data["direction"] == "newest"
    entries = data["entries"]
    assert len(entries) == 2
    # History: user "hello", assistant "world", user "how are you"
    # newest 2 reversed = [user "how are you", assistant "world"]
    assert entries[0]["role"] == "user"
    assert "how are you" in entries[0]["preview"]
    assert entries[1]["role"] == "assistant"
    assert "world" in entries[1]["preview"]


@pytest.mark.asyncio
async def test_read_conversation_history_oldest_direction(
    tmp_path: Path,
) -> None:
    """B-ContextLoss-3: direction=oldest walks from the start."""
    from xmclaw.daemon.session_store import SessionStore
    db = tmp_path / "sess.db"
    store = SessionStore(db)
    from xmclaw.providers.llm.base import Message
    store.save("sess-def", [
        Message(role="user", content="first"),
        Message(role="assistant", content="second"),
        Message(role="user", content="third"),
    ])

    tools = BuiltinTools(session_store=store)
    call = ToolCall(
        name="read_conversation_history",
        args={"limit": 2, "direction": "oldest", "offset": 0},
        provenance="synthetic", session_id="sess-def",
    )
    result = await tools.invoke(call)
    assert result.ok is True
    data = result.content
    entries = data["entries"]
    assert entries[0]["role"] == "user"
    assert "first" in entries[0]["preview"]
    assert entries[1]["role"] == "assistant"
    assert "second" in entries[1]["preview"]


@pytest.mark.asyncio
async def test_read_conversation_history_no_store_refuses() -> None:
    """B-ContextLoss-3: calling the tool without a wired store returns
    a structured error."""
    tools = BuiltinTools()
    call = ToolCall(
        name="read_conversation_history",
        args={},
        provenance="synthetic", session_id="sess-x",
    )
    result = await tools.invoke(call)
    assert result.ok is False
    assert "not configured" in (result.error or "")
