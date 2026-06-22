"""Unit tests for canvas_create / canvas_update / canvas_close tools."""
from __future__ import annotations

import pytest

from xmclaw.core.bus import EventType
from xmclaw.core.ir import ToolCall
from xmclaw.daemon.session_store import SessionStore
from xmclaw.providers.tool.builtin import BuiltinTools


def _call(name: str, args: dict, *, session_id: str = "sess-x") -> ToolCall:
    return ToolCall(id="c1", name=name, args=args, provenance="synthetic", session_id=session_id)


@pytest.mark.asyncio
async def test_canvas_create_emits_event() -> None:
    seen: list[tuple[str, dict]] = []

    def _listener(event_type: str, payload: dict) -> None:
        seen.append((event_type, payload))

    tools = BuiltinTools(canvas_listener=_listener)
    out = await tools.invoke(
        _call("canvas_create", {"kind": "mermaid", "title": "Flow", "content": "graph TD;A-->B"})
    )
    assert out.ok is True
    assert "art_" in out.content
    assert len(seen) == 1
    assert seen[0][0] == EventType.CANVAS_ARTIFACT_CREATED
    assert seen[0][1]["kind"] == "mermaid"
    assert seen[0][1]["title"] == "Flow"
    assert seen[0][1]["content"] == "graph TD;A-->B"
    # 2026-06-06 regression: the event MUST carry the originating
    # session_id so the daemon's per-socket forwarder routes it to the
    # right chat. Without this the diagram never reached the browser.
    assert seen[0][1]["session_id"] == "sess-x"


@pytest.mark.asyncio
async def test_canvas_events_carry_session_id() -> None:
    """All three canvas mutations must stamp the real session_id in the
    payload — the daemon listener reads it to route the bus event to the
    originating WebSocket (see factory._canvas_listener)."""
    seen: list[tuple[str, dict]] = []

    def _listener(event_type: str, payload: dict) -> None:
        seen.append((event_type, payload))

    tools = BuiltinTools(canvas_listener=_listener)
    create = await tools.invoke(
        _call(
            "canvas_create",
            {"kind": "mermaid", "title": "F", "content": "graph TD;A-->B"},
            session_id="room-7",
        )
    )
    art_id = seen[0][1]["artifact_id"]
    await tools.invoke(
        _call("canvas_update", {"artifact_id": art_id, "content": "graph TD;A-->C"}, session_id="room-7")
    )
    await tools.invoke(
        _call("canvas_close", {"artifact_id": art_id}, session_id="room-7")
    )
    assert [e[0] for e in seen] == [
        EventType.CANVAS_ARTIFACT_CREATED,
        EventType.CANVAS_ARTIFACT_UPDATED,
        EventType.CANVAS_ARTIFACT_CLOSED,
    ]
    for _etype, payload in seen:
        assert payload["session_id"] == "room-7", payload
    assert create.ok is True


@pytest.mark.asyncio
async def test_canvas_create_rejects_invalid_kind() -> None:
    tools = BuiltinTools()
    out = await tools.invoke(
        _call("canvas_create", {"kind": "invalid", "title": "X", "content": "x"})
    )
    assert out.ok is False
    assert "invalid kind" in out.content.lower()


@pytest.mark.asyncio
async def test_canvas_create_rejects_missing_title() -> None:
    tools = BuiltinTools()
    out = await tools.invoke(
        _call("canvas_create", {"kind": "mermaid", "title": "", "content": "x"})
    )
    assert out.ok is False
    assert "title" in out.content.lower()


@pytest.mark.asyncio
async def test_canvas_update_mutates_and_emits() -> None:
    seen: list[tuple[str, dict]] = []

    def _listener(event_type: str, payload: dict) -> None:
        seen.append((event_type, payload))

    tools = BuiltinTools(canvas_listener=_listener)
    create_out = await tools.invoke(
        _call("canvas_create", {"kind": "table", "title": "T", "content": "{\"rows\":[]}"})
    )
    assert create_out.ok is True
    art_id = seen[0][1]["artifact_id"]

    update_out = await tools.invoke(
        _call("canvas_update", {"artifact_id": art_id, "content": "{\"rows\":[1]}"})
    )
    assert update_out.ok is True
    assert len(seen) == 2
    assert seen[1][0] == EventType.CANVAS_ARTIFACT_UPDATED
    assert seen[1][1]["artifact_id"] == art_id


@pytest.mark.asyncio
async def test_canvas_update_unknown_artifact_fails() -> None:
    tools = BuiltinTools()
    out = await tools.invoke(
        _call("canvas_update", {"artifact_id": "art_nope", "content": "x"})
    )
    assert out.ok is False
    assert "not found" in out.content.lower()


@pytest.mark.asyncio
async def test_canvas_close_removes_and_emits() -> None:
    seen: list[tuple[str, dict]] = []

    def _listener(event_type: str, payload: dict) -> None:
        seen.append((event_type, payload))

    tools = BuiltinTools(canvas_listener=_listener)
    create_out = await tools.invoke(
        _call("canvas_create", {"kind": "html", "title": "H", "content": "<p>hi</p>"})
    )
    art_id = seen[0][1]["artifact_id"]

    close_out = await tools.invoke(_call("canvas_close", {"artifact_id": art_id}))
    assert close_out.ok is True
    assert len(seen) == 2
    assert seen[1][0] == EventType.CANVAS_ARTIFACT_CLOSED
    assert seen[1][1]["artifact_id"] == art_id


@pytest.mark.asyncio
async def test_canvas_close_unknown_artifact_fails() -> None:
    tools = BuiltinTools()
    out = await tools.invoke(_call("canvas_close", {"artifact_id": "art_nope"}))
    assert out.ok is False
    assert "not found" in out.content.lower()


@pytest.mark.asyncio
async def test_canvas_tools_are_listed() -> None:
    tools = BuiltinTools()
    specs = tools.list_tools()
    names = {s.name for s in specs}
    assert "canvas_create" in names
    assert "canvas_update" in names
    assert "canvas_close" in names


@pytest.mark.asyncio
async def test_canvas_session_isolation() -> None:
    tools = BuiltinTools()
    out1 = await tools.invoke(
        _call("canvas_create", {"kind": "svg", "title": "S1", "content": "<svg/>"}, session_id="alpha")
    )
    assert out1.ok is True
    out2 = await tools.invoke(
        _call("canvas_create", {"kind": "svg", "title": "S2", "content": "<svg/>"}, session_id="beta")
    )
    assert out2.ok is True

    # beta should not be able to update alpha's artifact
    reg = tools._ensure_canvas_registry()
    alpha_arts = list(reg.get("alpha", {}).keys())
    beta_arts = list(reg.get("beta", {}).keys())
    assert len(alpha_arts) == 1
    assert len(beta_arts) == 1
    assert alpha_arts[0] != beta_arts[0]

    fail = await tools.invoke(
        _call("canvas_update", {"artifact_id": alpha_arts[0], "content": "x"}, session_id="beta")
    )
    assert fail.ok is False
    assert "not found" in fail.content.lower()


# ── Wave-36 persistence tests ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_canvas_create_persists_to_session_store(tmp_path) -> None:
    """Creating an artifact writes it into the SQLite session_store."""
    db = tmp_path / "sess.db"
    store = SessionStore(db)
    tools = BuiltinTools(session_store=store)

    out = await tools.invoke(
        _call("canvas_create", {"kind": "mermaid", "title": "Flow", "content": "graph TD;A-->B"})
    )
    assert out.ok is True
    art_id = out.content.split("ID: ")[1].split(" —")[0]

    art = store.get_canvas_artifact(art_id)
    assert art is not None
    assert art["kind"] == "mermaid"
    assert art["title"] == "Flow"
    assert art["content"] == "graph TD;A-->B"
    assert art["session_id"] == "sess-x"


@pytest.mark.asyncio
async def test_canvas_survives_instance_rebuild(tmp_path) -> None:
    """A new BuiltinTools instance wired to the same store can update and close
    artifacts created by a previous instance."""
    db = tmp_path / "sess.db"
    store = SessionStore(db)

    # Instance 1: create
    tools1 = BuiltinTools(session_store=store)
    create_out = await tools1.invoke(
        _call("canvas_create", {"kind": "chart", "title": "C", "content": "{}"}, session_id="room-1")
    )
    assert create_out.ok is True
    art_id = create_out.content.split("ID: ")[1].split(" —")[0]

    # Instance 2: update (simulates daemon restart or agent rebuild)
    tools2 = BuiltinTools(session_store=store)
    update_out = await tools2.invoke(
        _call("canvas_update", {"artifact_id": art_id, "content": "{\"v\":2}"}, session_id="room-1")
    )
    assert update_out.ok is True

    # Instance 3: close
    tools3 = BuiltinTools(session_store=store)
    close_out = await tools3.invoke(
        _call("canvas_close", {"artifact_id": art_id}, session_id="room-1")
    )
    assert close_out.ok is True

    # Should be gone from DB
    assert store.get_canvas_artifact(art_id) is None


@pytest.mark.asyncio
async def test_canvas_close_deletes_from_session_store(tmp_path) -> None:
    """Closing an artifact removes it from the persistent store."""
    db = tmp_path / "sess.db"
    store = SessionStore(db)
    tools = BuiltinTools(session_store=store)

    out = await tools.invoke(
        _call("canvas_create", {"kind": "table", "title": "T", "content": "{\"rows\":[]}"})
    )
    assert out.ok is True
    art_id = out.content.split("ID: ")[1].split(" —")[0]

    assert store.get_canvas_artifact(art_id) is not None
    close_out = await tools.invoke(_call("canvas_close", {"artifact_id": art_id}))
    assert close_out.ok is True
    assert store.get_canvas_artifact(art_id) is None


@pytest.mark.asyncio
async def test_canvas_hydration_from_db(tmp_path) -> None:
    """When a new instance touches a session it hydrates the in-memory registry
    from the DB, preventing false "not found" errors."""
    db = tmp_path / "sess.db"
    store = SessionStore(db)

    # Create with instance 1
    tools1 = BuiltinTools(session_store=store)
    await tools1.invoke(
        _call("canvas_create", {"kind": "svg", "title": "S", "content": "<svg/>"}, session_id="s1")
    )
    # Bypass the in-memory registry to simulate a fresh process
    arts = store.list_canvas_artifacts("s1")
    assert len(arts) == 1
    art_id = arts[0]["artifact_id"]

    # Fresh instance should be able to update without any prior memory state
    tools2 = BuiltinTools(session_store=store)
    # Force a clear registry to mimic "never touched this session"
    tools2._canvas_registry = {}
    tools2._canvas_hydrated = set()
    update_out = await tools2.invoke(
        _call("canvas_update", {"artifact_id": art_id, "content": "<svg><circle/></svg>"}, session_id="s1")
    )
    assert update_out.ok is True
    # Verify DB also reflects the mutation
    art = store.get_canvas_artifact(art_id)
    assert art is not None
    assert art["content"] == "<svg><circle/></svg>"


@pytest.mark.asyncio
async def test_canvas_persistence_is_session_scoped(tmp_path) -> None:
    """Artifacts are scoped to session_id; cross-session access is blocked
    even when both sessions share the same store."""
    db = tmp_path / "sess.db"
    store = SessionStore(db)
    tools = BuiltinTools(session_store=store)

    out = await tools.invoke(
        _call("canvas_create", {"kind": "mermaid", "title": "Alpha", "content": "A"}, session_id="alpha")
    )
    assert out.ok is True
    art_id = out.content.split("ID: ")[1].split(" —")[0]

    # Same store, different session → not found
    fail = await tools.invoke(
        _call("canvas_update", {"artifact_id": art_id, "content": "B"}, session_id="beta")
    )
    assert fail.ok is False
    assert "not found" in fail.content.lower()
    # The artifact should still exist in alpha
    assert store.get_canvas_artifact(art_id) is not None
    assert store.get_canvas_artifact(art_id)["session_id"] == "alpha"
