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


def test_evolution_status_enriches_skill_metadata_from_sidecar(tmp_path, monkeypatch):
    """``/api/evolution/status`` must read human-readable metadata (name,
    category, version, description) from the ``skill_*.json`` sidecar
    written by SkillForge. Without this the Evolution page renders a wall
    of raw hex IDs like ``skill_01ae10a3`` and operators can't tell the
    skills apart — the exact bug that made the 进化 page "太乱".

    Each row must expose both ``id`` (filename stem for URLs) and ``name``
    (human-readable) so the frontend can show the name while routing
    ``/api/evolution/entity/skill/<id>`` lookups to the right file.
    """
    import xmclaw.daemon.server as srv

    fake_base = tmp_path / "repo"
    skills_dir = fake_base / "shared" / "skills"
    skills_dir.mkdir(parents=True)
    # Sidecar-present skill — backend should prefer JSON over parsing .py
    (skills_dir / "skill_abc123ef.py").write_text(
        'class Frequent:\n    name = "skill_abc123ef"\n', encoding="utf-8"
    )
    (skills_dir / "skill_abc123ef.json").write_text(json.dumps({
        "id": "skill_abc123ef",
        "name": "auto_frequent_bash_usage",
        "category": "auto",
        "version": "v1",
        "description": "Tool 'bash' was used 10 times recently.",
    }), encoding="utf-8")
    # No-sidecar skill — backend should still surface *something*, not crash
    (skills_dir / "skill_deadbeef.py").write_text(
        '"""legacy skill without sidecar"""\n', encoding="utf-8"
    )

    monkeypatch.setattr(srv, "BASE_DIR", fake_base)

    with TestClient(app) as client:
        resp = client.get("/api/evolution/status")
        assert resp.status_code == 200
        skills = {s["id"]: s for s in resp.json().get("skills", [])}

        enriched = skills.get("skill_abc123ef")
        assert enriched is not None, "skill with sidecar must be listed"
        assert enriched["name"] == "auto_frequent_bash_usage"
        assert enriched["category"] == "auto"
        assert enriched["version"] == "v1"
        assert "bash" in enriched["description"]

        legacy = skills.get("skill_deadbeef")
        assert legacy is not None, "skill without sidecar must still be listed"
        # No sidecar → name falls back to stem; frontend then shows the ID.
        assert legacy["name"] == "skill_deadbeef"


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


def test_workspace_files_api_shows_identity_hides_secrets(tmp_path, monkeypatch):
    """The 工作区 view is the user's window onto *who this agent is*, so
    it must include identity files (``SOUL.md``, ``PROFILE.md``,
    ``AGENTS.md``) plus the ``workspace/`` subfolder — but it must
    still hide API keys (``agent.json``) and daemon internals
    (``memory/``).

    Two-sided regression guard:
    - Old failure: PR #17 rooted the view at ``workspace/`` and hid
      SOUL/PROFILE/AGENTS — users couldn't see or edit the files
      that define the agent.
    - Opposite failure: exposing ``agent.json`` would leak API keys
      to anyone who can hit the daemon.
    """
    import xmclaw.daemon.server as srv
    from pathlib import Path as _P

    agents_root = tmp_path / "agents"
    agent_dir = agents_root / "iso"
    (agent_dir / "memory" / "sessions").mkdir(parents=True)
    (agent_dir / "workspace").mkdir(parents=True)
    (agent_dir / "agent.json").write_text('{"api_key": "SECRET"}', encoding="utf-8")
    (agent_dir / "agent.example.json").write_text("{}", encoding="utf-8")
    (agent_dir / "SOUL.md").write_text("soul", encoding="utf-8")
    (agent_dir / "PROFILE.md").write_text("profile", encoding="utf-8")
    (agent_dir / "AGENTS.md").write_text("agents", encoding="utf-8")
    (agent_dir / "workspace" / "notes.md").write_text("real", encoding="utf-8")

    monkeypatch.setattr(srv, "AGENTS_DIR", agents_root)

    with TestClient(app) as client:
        resp = client.get("/api/agent/iso/files")
        assert resp.status_code == 200
        entries = resp.json()["files"]
        names = {e["path"] for e in entries}

        # Identity files (the point of the workspace) MUST be visible.
        assert "SOUL.md" in names
        assert "PROFILE.md" in names
        assert "AGENTS.md" in names
        # workspace/notes.md must still be listed via nested path.
        assert any(p.endswith("notes.md") for p in names), names

        # Secrets and daemon internals MUST NOT leak.
        assert "agent.json" not in names, "agent.json carries API keys"
        assert "agent.example.json" not in names
        assert not any(_P(p).parts[0] == "memory" for p in names), names

        # And the file-read endpoint must refuse agent.json even by direct path.
        bad = client.get("/api/agent/iso/file", params={"path": "agent.json"})
        assert bad.status_code == 403

        # But SOUL.md must be readable.
        good = client.get("/api/agent/iso/file", params={"path": "SOUL.md"})
        assert good.status_code == 200
        assert good.json()["content"] == "soul"


def test_workspace_files_api_returns_posix_paths(tmp_path, monkeypatch):
    """Nested entries must come back with forward-slash separators.

    Regression guard: on Windows ``str(Path('workspace') / 'notes.md')``
    yields ``workspace\\notes.md``. The web UI splits on ``/`` to
    reconstruct hierarchy — with backslashes every nested file was
    rendered at the tree root and ``workspace/`` looked empty. The
    symptom the user reported as "文件夹是无效的".
    """
    import xmclaw.daemon.server as srv

    agents_root = tmp_path / "agents"
    agent_dir = agents_root / "isoposix"
    (agent_dir / "workspace").mkdir(parents=True)
    (agent_dir / "SOUL.md").write_text("s", encoding="utf-8")
    (agent_dir / "workspace" / "notes.md").write_text("n", encoding="utf-8")
    (agent_dir / "workspace" / "tasks.json").write_text("[]", encoding="utf-8")

    monkeypatch.setattr(srv, "AGENTS_DIR", agents_root)

    with TestClient(app) as client:
        resp = client.get("/api/agent/isoposix/files")
        assert resp.status_code == 200
        paths = [e["path"] for e in resp.json()["files"]]

        # Every nested path uses forward slashes — no literal backslash
        # anywhere, on any platform.
        assert not any("\\" in p for p in paths), paths
        assert "workspace/notes.md" in paths
        assert "workspace/tasks.json" in paths


# Async tool tests

@pytest.mark.asyncio
async def test_tool_registry_executes_file_write_and_read():
    """ToolRegistry can write and read a file."""
    # Allow file_write in tests (PermissionManager default is ASK → blocked)
    from xmclaw.utils.security import get_permission_manager, PermissionLevel
    pm = get_permission_manager()
    pm.set_tool_permission("file_write", PermissionLevel.ALLOW)

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
        pm.set_tool_permission("file_write", PermissionLevel.ASK)  # restore


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
async def test_task_and_todo_tools_honor_agent_id(tmp_path, monkeypatch):
    """Tasks/todos for agent ``a`` must not leak into agent ``b``.

    Regression guard: both tools previously hard-coded
    ``BASE_DIR/agents/default/workspace/...``, so in a multi-agent setup
    every write landed in the default agent's workspace silently. After
    this fix the registry forwards ``agent_id`` into the tools and each
    agent owns its own tasks.json / todos.json.
    """
    import xmclaw.tools.task_tool as task_mod
    import xmclaw.tools.todo as todo_mod
    import xmclaw.utils.paths as paths_mod

    fake_base = tmp_path / "repo"
    monkeypatch.setattr(paths_mod, "BASE_DIR", fake_base)
    # get_agent_dir reads BASE_DIR at call time via module-level access
    # in the tools, so the patch is sufficient.

    reg = ToolRegistry()
    await reg.load_all()

    await reg.execute("task", {"action": "create", "title": "A-only"}, agent_id="a")
    await reg.execute("todo", {"action": "add", "text": "todo-a"}, agent_id="a")

    list_b_tasks = await reg.execute("task", {"action": "list"}, agent_id="b")
    list_b_todos = await reg.execute("todo", {"action": "list"}, agent_id="b")
    assert "A-only" not in list_b_tasks
    assert "todo-a" not in list_b_todos

    list_a_tasks = await reg.execute("task", {"action": "list"}, agent_id="a")
    list_a_todos = await reg.execute("todo", {"action": "list"}, agent_id="a")
    assert "A-only" in list_a_tasks
    assert "todo-a" in list_a_todos

    # And the files live in agent_a's workspace, not anywhere under agent_b.
    assert (fake_base / "agents" / "a" / "workspace" / "tasks.json").exists()
    assert (fake_base / "agents" / "a" / "workspace" / "todos.json").exists()
    assert not (fake_base / "agents" / "b" / "workspace" / "tasks.json").exists()
    assert not (fake_base / "agents" / "b" / "workspace" / "todos.json").exists()


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


@pytest.mark.asyncio
async def test_file_edit_append_mode_creates_and_extends():
    """file_edit with mode='append' creates missing files and appends to existing.

    The system prompt tells the agent to append to ``decisions.md`` via
    ``file_edit``. Before this fix file_edit only did find-and-replace,
    so the instruction was a no-op — the agent had to read/concat/write
    itself. Now append is a real mode.
    """
    from xmclaw.utils.security import get_permission_manager, PermissionLevel
    pm = get_permission_manager()
    pm.set_tool_permission("file_edit", PermissionLevel.ALLOW)

    reg = ToolRegistry()
    await reg.load_all()
    target = BASE_DIR / "integration_append_test.md"
    try:
        r1 = await reg.execute("file_edit", {
            "file_path": str(target),
            "new_text": "first line\n",
            "mode": "append",
        })
        assert "appended" in r1.lower()
        assert target.read_text(encoding="utf-8") == "first line\n"

        r2 = await reg.execute("file_edit", {
            "file_path": str(target),
            "new_text": "second line\n",
            "mode": "append",
        })
        assert "appended" in r2.lower()
        assert target.read_text(encoding="utf-8") == "first line\nsecond line\n"

        # Replace mode still works and still needs old_text
        r3 = await reg.execute("file_edit", {
            "file_path": str(target),
            "old_text": "second",
            "new_text": "SECOND",
        })
        assert "edited" in r3.lower()
        assert "SECOND line" in target.read_text(encoding="utf-8")

        # Missing old_text in replace mode is a user-visible error, not a crash
        r4 = await reg.execute("file_edit", {
            "file_path": str(target),
            "new_text": "x",
        })
        assert "old_text is required" in r4
    finally:
        if target.exists():
            target.unlink()
        pm.set_tool_permission("file_edit", PermissionLevel.ASK)
