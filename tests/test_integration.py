"""End-to-end integration tests for XMclaw core flows."""
import asyncio
import json
import pytest
from pathlib import Path

from fastapi.testclient import TestClient
from xmclaw.daemon.server import app
from xmclaw.tools.registry import ToolRegistry
from xmclaw.utils.paths import BASE_DIR, get_agent_dir


# Synchronous API tests using TestClient

def test_health_endpoint():
    """Daemon health check returns ok."""
    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


def test_agent_config_api():
    """Agent config can be read via API."""
    with TestClient(app) as client:
        resp = client.get("/api/agent/default/config")
        assert resp.status_code in (200, 404)


def test_todo_api_roundtrip():
    """Todos can be written and read back."""
    with TestClient(app) as client:
        payload = [{"text": "integration test todo", "done": False}]
        post = client.post("/api/agent/default/todos", json=payload)
        assert post.status_code == 200
        get = client.get("/api/agent/default/todos")
        assert get.status_code == 200
        data = get.json()
        assert any(item.get("text") == "integration test todo" for item in data)


def test_evolution_status_api():
    """Evolution status endpoint returns counts."""
    with TestClient(app) as client:
        resp = client.get("/api/evolution/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "gene_count" in data
        assert "skill_count" in data


def test_tool_execution_api():
    """Generic tool execution API works for bash."""
    with TestClient(app) as client:
        resp = client.post("/api/agent/default/tools/bash", json={"command": "echo api_test"})
        assert resp.status_code == 200
        assert "api_test" in resp.json().get("result", "")


def test_workspace_files_api():
    """Workspace files API returns a list."""
    with TestClient(app) as client:
        resp = client.get("/api/agent/default/files")
        assert resp.status_code in (200, 404)


# Async tool tests

@pytest.mark.asyncio
async def test_tool_registry_executes_file_write_and_read():
    """ToolRegistry can write and read a file."""
    reg = ToolRegistry()
    await reg.load_all()
    test_path = BASE_DIR / "integration_test_file.txt"
    try:
        write_result = await reg.execute("file_write", {
            "file_path": str(test_path),
            "content": "integration content"
        })
        assert "File written" in write_result
        read_result = await reg.execute("file_read", {
            "file_path": str(test_path)
        })
        assert read_result == "integration content"
    finally:
        if test_path.exists():
            test_path.unlink()


@pytest.mark.asyncio
async def test_bash_tool_echo():
    """Bash tool can execute a simple echo."""
    reg = ToolRegistry()
    await reg.load_all()
    result = await reg.execute("bash", {"command": "echo integration_echo"})
    assert "integration_echo" in result


@pytest.mark.asyncio
async def test_todo_tool_crud():
    """Todo tool supports add and list."""
    reg = ToolRegistry()
    await reg.load_all()
    agent_dir = get_agent_dir("default")
    todo_path = agent_dir / "workspace" / "todos.json"
    try:
        await reg.execute("todo", {"action": "add", "text": "integration todo"})
        result = await reg.execute("todo", {"action": "list"})
        assert "integration todo" in result
    finally:
        if todo_path.exists():
            todo_path.unlink()


@pytest.mark.asyncio
async def test_memory_search_returns_string():
    """Memory search tool returns a string result."""
    reg = ToolRegistry()
    await reg.load_all()
    result = await reg.execute("memory_search", {"query": "integration", "top_k": 3})
    assert isinstance(result, str)


@pytest.mark.asyncio
async def test_git_tool_status():
    """Git tool can run status in the project directory."""
    reg = ToolRegistry()
    await reg.load_all()
    result = await reg.execute("git", {"command": "status", "cwd": str(BASE_DIR)})
    assert isinstance(result, str)


@pytest.mark.asyncio
async def test_test_tool_run_specific():
    """Test tool can run a specific fast test file."""
    reg = ToolRegistry()
    await reg.load_all()
    result = await reg.execute("test", {"action": "run", "target": "tests/test_security.py"})
    assert "passed" in result
