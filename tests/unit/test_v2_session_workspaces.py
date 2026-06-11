"""F1 (2026-05-30) — per-session workspace: manager + HTTP surface.

Per the front-back boundary rule (CLAUDE.md, 2026-05-09): the router
tests exercise the REAL ``create_app`` over TestClient — the same HTTP
path the WorkspacePanel fetches — not router internals.

Pins:
  * WorkspaceManager: side-effect filtering (only workspace-contained
    paths trigger events), git auto-commit, containment in resolve_safe
  * /tree returns entries for a session with files (and [] before any)
  * /file reads UTF-8 content; rejects escapes; 404 on missing
  * /raw serves bytes with a mime type; 404 on escape attempts
  * /commits + /diff round-trip after a tracked change
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from xmclaw.core.bus.events import EventType, make_event
from xmclaw.core.bus.memory import InProcessEventBus
from xmclaw.daemon.app import create_app
from xmclaw.daemon.workspace_manager import WorkspaceManager


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Reroute ~/.xmclaw to a temp dir so tests never touch real state."""
    monkeypatch.setenv("XMC_DATA_DIR", str(tmp_path / "data"))
    yield


# ── manager-level ────────────────────────────────────────────────────


def test_resolve_safe_blocks_escapes() -> None:
    bus = InProcessEventBus()
    mgr = WorkspaceManager(bus=bus)
    sid = "sess-escape"
    ws = mgr.ensure_dir(sid)
    (ws / "ok.txt").write_text("fine", encoding="utf-8")

    assert mgr.resolve_safe(sid, "ok.txt") is not None
    assert mgr.resolve_safe(sid, "../../../etc/passwd") is None
    assert mgr.resolve_safe(sid, "C:/Windows/win.ini") is None
    assert mgr.resolve_safe(sid, "missing.txt") is None


def test_tool_finished_outside_workspace_ignored(tmp_path: Path) -> None:
    """A file_write landing OUTSIDE the session workspace must not
    produce a WORKSPACE_FILE_CHANGED event."""
    bus = InProcessEventBus()
    mgr = WorkspaceManager(bus=bus)
    mgr.start()
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("not workspace", encoding="utf-8")

    seen: list = []
    bus.subscribe(
        lambda e: e.type == EventType.WORKSPACE_FILE_CHANGED,
        # handler must be async per bus contract
        _collector(seen),
    )

    async def run() -> None:
        await bus.publish(make_event(
            session_id="sess-x",
            agent_id="t",
            type=EventType.TOOL_INVOCATION_FINISHED,
            payload={
                "name": "file_write",
                "expected_side_effects": [str(outside.resolve())],
                "ok": True,
            },
        ))
        await bus.drain()
        await asyncio.sleep(0.1)

    asyncio.run(run())
    assert seen == []


def test_tool_finished_inside_workspace_emits_event() -> None:
    bus = InProcessEventBus()
    mgr = WorkspaceManager(bus=bus)
    mgr.start()
    sid = "sess-evt"
    ws = mgr.ensure_dir(sid)
    f = ws / "note.md"
    f.write_text("# hello", encoding="utf-8")

    seen: list = []
    bus.subscribe(
        lambda e: e.type == EventType.WORKSPACE_FILE_CHANGED,
        _collector(seen),
    )

    async def run() -> None:
        await bus.publish(make_event(
            session_id=sid,
            agent_id="t",
            type=EventType.TOOL_INVOCATION_FINISHED,
            payload={
                "name": "file_write",
                "expected_side_effects": [str(f.resolve())],
                "ok": True,
            },
        ))
        await bus.drain()
        # Git work runs in a thread; give it a beat.
        await asyncio.sleep(0.5)

    asyncio.run(run())
    assert len(seen) == 1
    payload = seen[0].payload
    assert payload["rel_path"] == "note.md"
    assert payload["action"] in ("created", "modified")
    assert payload["tool"] == "file_write"


def _collector(sink: list):
    async def _handler(event) -> None:
        sink.append(event)
    return _handler


# ── HTTP surface (real create_app — front-back boundary rule) ────────


@pytest.fixture
def client_and_mgr():
    bus = InProcessEventBus()
    app = create_app(bus=bus)
    mgr = WorkspaceManager(bus=bus)
    app.state.workspace_manager = mgr
    with TestClient(app) as c:
        yield c, mgr


def test_tree_empty_then_populated(client_and_mgr) -> None:
    c, mgr = client_and_mgr
    sid = "sess-http-tree"

    r = c.get(f"/api/v2/session_workspaces/{sid}/tree")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "entries": []}

    ws = mgr.ensure_dir(sid)
    (ws / "a.md").write_text("# a", encoding="utf-8")
    (ws / "sub").mkdir()
    (ws / "sub" / "b.py").write_text("print(1)", encoding="utf-8")

    r = c.get(f"/api/v2/session_workspaces/{sid}/tree")
    rels = [e["rel_path"] for e in r.json()["entries"]]
    assert "a.md" in rels
    assert "sub/b.py" in rels


def test_file_read_and_escape(client_and_mgr) -> None:
    c, mgr = client_and_mgr
    sid = "sess-http-file"
    ws = mgr.ensure_dir(sid)
    (ws / "x.txt").write_text("content here", encoding="utf-8")

    r = c.get(f"/api/v2/session_workspaces/{sid}/file", params={"path": "x.txt"})
    assert r.status_code == 200
    assert r.json()["content"] == "content here"

    r = c.get(f"/api/v2/session_workspaces/{sid}/file", params={"path": "../escape.txt"})
    assert r.status_code in (400, 404)

    r = c.get(f"/api/v2/session_workspaces/{sid}/file", params={"path": "missing.txt"})
    assert r.status_code == 404


def test_raw_serves_mime_and_blocks_escape(client_and_mgr) -> None:
    c, mgr = client_and_mgr
    sid = "sess-http-raw"
    ws = mgr.ensure_dir(sid)
    (ws / "page.html").write_text("<h1>hi</h1>", encoding="utf-8")
    # 1x1 transparent PNG
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000d4944415478da63fcff9fa10e0003030101a0a8f2cd0000000049454e44ae426082"
    )
    (ws / "dot.png").write_bytes(png)

    r = c.get(f"/api/v2/session_workspaces/{sid}/raw", params={"path": "page.html"})
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert b"<h1>hi</h1>" in r.content

    r = c.get(f"/api/v2/session_workspaces/{sid}/raw", params={"path": "dot.png"})
    assert r.status_code == 200
    assert "image/png" in r.headers["content-type"]

    r = c.get(f"/api/v2/session_workspaces/{sid}/raw", params={"path": "../../pairing_token.txt"})
    assert r.status_code == 404


def test_commits_and_diff_roundtrip(client_and_mgr) -> None:
    """Full pipeline over HTTP: simulated tool write → manager commit →
    /commits lists it → /diff returns the patch."""
    import shutil
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    c, mgr = client_and_mgr
    mgr.start()
    sid = "sess-http-git"
    ws = mgr.ensure_dir(sid)
    f = ws / "draft.md"
    f.write_text("v1 content", encoding="utf-8")

    async def fire() -> None:
        await mgr._on_tool_finished(make_event(
            session_id=sid,
            agent_id="t",
            type=EventType.TOOL_INVOCATION_FINISHED,
            payload={
                "name": "file_write",
                "expected_side_effects": [str(f.resolve())],
                "ok": True,
            },
        ))

    asyncio.run(fire())

    r = c.get(f"/api/v2/session_workspaces/{sid}/commits")
    assert r.status_code == 200
    commits = r.json()["commits"]
    assert any("draft.md" in (cm.get("subject") or "") for cm in commits)

    sha = commits[0]["sha"]
    r = c.get(f"/api/v2/session_workspaces/{sid}/diff", params={"commit": sha})
    assert r.status_code == 200
    assert "v1 content" in r.json()["diff"]

    # Bad sha shapes rejected without invoking git.
    r = c.get(f"/api/v2/session_workspaces/{sid}/diff", params={"commit": "$(rm -rf)"})
    assert r.status_code == 400


def test_diff_with_non_ascii_content(client_and_mgr) -> None:
    """Regression (2026-06-11): on zh-CN Windows, subprocess text=True
    decoded git output with GBK; a UTF-8 diff (Chinese / emoji bytes)
    blew up the reader thread → stdout=None → /diff returned ok=True
    with an EMPTY diff and the 改动 tab rendered blank. _run_git must
    decode as UTF-8 regardless of locale."""
    import shutil
    if shutil.which("git") is None:
        pytest.skip("git not on PATH")
    c, mgr = client_and_mgr
    mgr.start()
    sid = "sess-http-utf8"
    ws = mgr.ensure_dir(sid)
    f = ws / "notes.py"
    f.write_text('# 中文注释 🚀 émoji\nprint("日志输出")\n', encoding="utf-8")

    async def fire() -> None:
        await mgr._on_tool_finished(make_event(
            session_id=sid,
            agent_id="t",
            type=EventType.TOOL_INVOCATION_FINISHED,
            payload={
                "name": "file_write",
                "expected_side_effects": [str(f.resolve())],
                "ok": True,
            },
        ))

    asyncio.run(fire())

    r = c.get(f"/api/v2/session_workspaces/{sid}/commits")
    assert r.status_code == 200
    commits = r.json()["commits"]
    assert commits, "expected at least the notes.py auto-commit"

    sha = commits[0]["sha"]
    r = c.get(f"/api/v2/session_workspaces/{sid}/diff", params={"commit": sha})
    assert r.status_code == 200
    diff = r.json()["diff"]
    assert diff.strip(), "diff must not be empty for a non-ASCII change"
    assert "中文注释" in diff
    assert "notes.py" in diff
