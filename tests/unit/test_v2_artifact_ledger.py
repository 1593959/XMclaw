import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from xmclaw.cognition.artifact_ledger import (
    ArtifactLedger,
    ArtifactLedgerStore,
    event_to_artifacts,
)
from xmclaw.core.bus import EventType, InProcessEventBus, make_event
from xmclaw.core.ir import ToolCall
from xmclaw.daemon.routers import tasks as tasks_router
from xmclaw.providers.tool.builtin import BuiltinTools


def test_event_to_artifacts_extracts_side_effect_path() -> None:
    event = make_event(
        session_id="s1",
        agent_id="agent",
        type=EventType.TOOL_INVOCATION_FINISHED,
        payload={
            "call_id": "c1",
            "name": "browser_download",
            "ok": True,
            "expected_side_effects": [
                "downloaded to E:\\Downloads\\WeChatSetup-3.9.12.exe",
            ],
        },
    )

    records = event_to_artifacts(event)

    assert len(records) == 1
    assert records[0].artifact_type == "installer"
    assert records[0].path == "E:\\Downloads\\WeChatSetup-3.9.12.exe"
    assert records[0].target_drive == "E:"
    assert records[0].tool_name == "browser_download"


def test_event_to_artifacts_extracts_attachments_and_urls() -> None:
    event = make_event(
        session_id="s1",
        agent_id="agent",
        type=EventType.TOOL_INVOCATION_FINISHED,
        payload={
            "call_id": "c1",
            "name": "generate_image",
            "ok": True,
            "attachments": [
                {
                    "kind": "image",
                    "path": "C:\\tmp\\cover.png",
                    "url": "/api/v2/media/cover.png",
                    "mime": "image/png",
                },
            ],
            "documents": [
                {
                    "name": "report.pdf",
                    "url": "/api/v2/media/report.pdf",
                    "mime": "application/pdf",
                },
            ],
        },
    )

    records = event_to_artifacts(event)
    by_source = {r.source: r for r in records}

    assert by_source["attachment"].artifact_type == "image"
    assert by_source["attachment"].name == "cover.png"
    assert by_source["document"].artifact_type == "document"
    assert by_source["document"].name == "report.pdf"


@pytest.mark.asyncio
async def test_artifact_ledger_subscribes_and_persists(tmp_path) -> None:
    bus = InProcessEventBus()
    store = ArtifactLedgerStore(tmp_path / "artifacts.db")
    ledger = ArtifactLedger(bus=bus, store=store)
    ledger.start()

    await bus.publish(make_event(
        session_id="s1",
        agent_id="agent",
        type=EventType.TOOL_INVOCATION_FINISHED,
        payload={
            "call_id": "c1",
            "name": "file_write",
            "ok": True,
            "expected_side_effects": ["C:\\work\\notes.md"],
        },
    ))
    await bus.drain()

    rows = store.list_recent(session_id="s1")
    assert len(rows) == 1
    assert rows[0]["name"] == "notes.md"
    assert rows[0]["artifact_type"] == "document"


def test_tasks_artifacts_endpoint(tmp_path) -> None:
    app = FastAPI()
    app.include_router(tasks_router.router)
    store = ArtifactLedgerStore(tmp_path / "artifacts.db")
    app.state.artifact_ledger_store = store
    store.add_many(event_to_artifacts(make_event(
        session_id="s1",
        agent_id="agent",
        type=EventType.TOOL_INVOCATION_FINISHED,
        payload={
            "call_id": "c1",
            "name": "browser_download",
            "ok": True,
            "expected_side_effects": ["E:\\Downloads\\setup.exe"],
        },
    )))

    with TestClient(app) as client:
        resp = client.get("/api/v2/tasks/s1/artifacts")

    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert body["artifacts"][0]["target_drive"] == "E:"


def test_artifact_store_search_filters_by_query_and_drive(tmp_path) -> None:
    store = ArtifactLedgerStore(tmp_path / "artifacts.db")
    store.add_many(event_to_artifacts(make_event(
        session_id="s1",
        agent_id="agent",
        type=EventType.TOOL_INVOCATION_FINISHED,
        payload={
            "call_id": "c1",
            "name": "browser_download",
            "ok": True,
            "expected_side_effects": ["E:\\Downloads\\WeChatSetup.exe"],
        },
    )))

    rows = store.search(query="wechat", session_id="s1", target_drive="E:")

    assert len(rows) == 1
    assert rows[0]["name"] == "WeChatSetup.exe"


@pytest.mark.asyncio
async def test_artifact_ledger_tool_returns_structured_candidates(tmp_path) -> None:
    store = ArtifactLedgerStore(tmp_path / "artifacts.db")
    store.add_many(event_to_artifacts(make_event(
        session_id="s1",
        agent_id="agent",
        type=EventType.TOOL_INVOCATION_FINISHED,
        payload={
            "call_id": "c1",
            "name": "browser_download",
            "ok": True,
            "expected_side_effects": ["E:\\Downloads\\WeChatSetup.exe"],
        },
    )))
    tools = BuiltinTools(enable_bash=False, enable_web=False)
    tools.set_artifact_ledger_store(store)

    names = {spec.name for spec in tools.list_tools()}
    assert "artifact_ledger" in names

    result = await tools.invoke(ToolCall(
        name="artifact_ledger",
        args={"query": "wechat", "session_id": "s1"},
        provenance="synthetic",
        id="tc1",
    ))

    assert result.ok
    assert '"candidates"' in result.content
    assert "WeChatSetup.exe" in result.content
    assert '"skip_reasons": []' in result.content


@pytest.mark.asyncio
async def test_artifact_ledger_tool_reports_skip_reasons(tmp_path) -> None:
    tools = BuiltinTools(enable_bash=False, enable_web=False)
    tools.set_artifact_ledger_store(ArtifactLedgerStore(tmp_path / "artifacts.db"))

    result = await tools.invoke(ToolCall(
        name="artifact_ledger",
        args={"query": "missing", "session_id": "s1"},
        provenance="synthetic",
        id="tc1",
    ))

    assert result.ok
    assert "no artifact matched" in result.content
