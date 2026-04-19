"""FastAPI + WebSocket server for XMclaw daemon."""
import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from xmclaw.daemon.config import DaemonConfig
from xmclaw.core.orchestrator import AgentOrchestrator
from xmclaw.core.event_bus import Event, get_event_bus
from xmclaw.evolution.scheduler import EvolutionScheduler
from xmclaw.integrations.manager import IntegrationManager
from xmclaw.daemon.static import mount_static_files
from xmclaw.utils.log import logger
from xmclaw.utils.paths import BASE_DIR

config = DaemonConfig.load()
orchestrator = AgentOrchestrator()
# Pass the shared MemoryManager so evolution reads the same sessions as the orchestrator.
# Initialized lazily in lifespan() after orchestrator.initialize().
evo_scheduler = EvolutionScheduler(memory=orchestrator.memory)
integration_manager = IntegrationManager(config.integrations)

AGENTS_DIR = BASE_DIR / "agents"


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("daemon_starting")
    await orchestrator.initialize()

    # Install built-in event handlers (audit log, tool analytics)
    from xmclaw.core.event_bus import install_event_handlers
    install_event_handlers()

    if config.evolution.get("enabled", True):
        await evo_scheduler.start()

    # Wire orchestrator into integration manager and start enabled integrations
    integration_manager._orchestrator = orchestrator
    await integration_manager.start()

    yield

    logger.info("daemon_stopping")
    await integration_manager.stop()
    if config.evolution.get("enabled", True):
        evo_scheduler.stop()
    await orchestrator.shutdown()


app = FastAPI(title="XMclaw Daemon", version="0.1.0", lifespan=lifespan)
mount_static_files(app)


@app.get("/health")
async def health():
    return {"status": "ok", "version": "0.1.0"}


# Agent Config API
@app.get("/api/agent/{agent_id}/config")
async def get_agent_config(agent_id: str):
    path = AGENTS_DIR / agent_id / "agent.json"
    if not path.exists():
        return JSONResponse({"error": "Agent not found"}, status_code=404)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


@app.post("/api/agent/{agent_id}/config")
async def update_agent_config(agent_id: str, data: dict):
    path = AGENTS_DIR / agent_id / "agent.json"
    if not path.exists():
        return JSONResponse({"error": "Agent not found"}, status_code=404)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return {"status": "ok"}


# Todo API
@app.get("/api/agent/{agent_id}/todos")
async def get_todos(agent_id: str):
    path = AGENTS_DIR / agent_id / "workspace" / "todos.json"
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


@app.post("/api/agent/{agent_id}/todos")
async def update_todos(agent_id: str, request: Request):
    data = await request.json()
    workspace = AGENTS_DIR / agent_id / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    path = workspace / "todos.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return {"status": "ok"}


# Workspace Files API
FILE_TYPE_MAP = {
    ".py":    "python",
    ".js":    "javascript",
    ".ts":    "typescript",
    ".json":  "json",
    ".md":    "markdown",
    ".txt":   "text",
    ".yml":   "yaml",
    ".yaml":  "yaml",
    ".toml":  "config",
    ".ini":   "config",
    ".cfg":   "config",
    ".conf":  "config",
    ".sh":    "shell",
    ".bash":  "shell",
    ".zsh":   "shell",
    ".bat":   "batch",
    ".ps1":   "powershell",
    ".html":  "html",
    ".css":   "css",
    ".svg":   "image",
    ".png":   "image",
    ".jpg":   "image",
    ".jpeg":  "image",
    ".gif":   "image",
    ".ico":   "image",
    ".pdf":   "pdf",
    ".csv":   "data",
    ".sql":   "database",
    ".xml":   "xml",
    ".log":   "log",
    ".env":   "env",
    ".gitignore": "config",
    ".dockerignore": "config",
    ".editorconfig": "config",
}


def _file_type(path: str) -> str:
    name = path.lower()
    for ext, ft in FILE_TYPE_MAP.items():
        if name.endswith(ext):
            return ft
    return "file"


def _format_size(size: int) -> str:
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f}KB"
    return f"{size / (1024 * 1024):.1f}MB"


def _build_tree(agent_dir: Path) -> list[dict]:
    """Build a nested directory tree from agent_dir.

    Returns a flat list of entries with: path, type (dir|file),
    fileType (python/json/markdown/etc.), size, name, children (for dirs).
    """
    entries = []

    for root, dirs, files in os.walk(agent_dir):
        root_path = Path(root)
        rel_root = root_path.relative_to(agent_dir)

        # Directories
        for dname in sorted(dirs):
            if dname.startswith("."):
                continue
            entries.append({
                "path": str(rel_root / dname) if str(rel_root) != "." else dname,
                "name": dname,
                "type": "dir",
                "fileType": "folder",
            })

        # Files
        for fname in sorted(files):
            if fname.startswith("."):
                continue
            fpath = root_path / fname
            entries.append({
                "path": str(rel_root / fname) if str(rel_root) != "." else fname,
                "name": fname,
                "type": "file",
                "fileType": _file_type(fname),
                "size": fpath.stat().st_size,
                "sizeLabel": _format_size(fpath.stat().st_size),
            })

    # No global sort — os.walk already traverses depth-first in tree order.
    # The frontend rebuilds the hierarchy from flat paths, so we preserve
    # the walk order so parent dirs appear before their children.
    return entries


@app.get("/api/agent/{agent_id}/files")
async def list_files(agent_id: str):
    """Return workspace file tree with type information."""
    agent_dir = AGENTS_DIR / agent_id
    if not agent_dir.exists():
        return JSONResponse({"error": "Agent not found"}, status_code=404)
    tree = _build_tree(agent_dir)
    total_files = sum(1 for e in tree if e["type"] == "file")
    total_dirs  = sum(1 for e in tree if e["type"] == "dir")
    return {
        "files": tree,
        "summary": {"files": total_files, "dirs": total_dirs},
    }


@app.get("/api/agent/{agent_id}/file")
async def read_file(agent_id: str, path: str):
    agent_dir = AGENTS_DIR / agent_id
    target = (agent_dir / path).resolve()
    if not str(target).startswith(str(agent_dir.resolve())):
        return JSONResponse({"error": "Invalid path"}, status_code=403)
    if not target.exists():
        return JSONResponse({"error": "File not found"}, status_code=404)
    try:
        with open(target, "r", encoding="utf-8") as f:
            content = f.read()
        return {"content": content}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/agent/{agent_id}/file")
async def write_file(agent_id: str, path: str, data: dict):
    agent_dir = AGENTS_DIR / agent_id
    target = (agent_dir / path).resolve()
    if not str(target).startswith(str(agent_dir.resolve())):
        return JSONResponse({"error": "Invalid path"}, status_code=403)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        f.write(data.get("content", ""))
    return {"status": "ok"}


@app.post("/api/agent/{agent_id}/file/create")
async def create_file(agent_id: str, data: dict):
    """Create a new file or directory."""
    agent_dir = AGENTS_DIR / agent_id
    if not agent_dir.exists():
        return JSONResponse({"error": "Agent not found"}, status_code=404)

    rel_path = data.get("path", "").strip()
    is_dir = data.get("is_dir", False)

    if not rel_path or ".." in rel_path or rel_path.startswith("/"):
        return JSONResponse({"error": "Invalid path"}, status_code=400)

    target = (agent_dir / rel_path).resolve()
    if not str(target).startswith(str(agent_dir.resolve())):
        return JSONResponse({"error": "Path outside workspace"}, status_code=403)

    if target.exists():
        return JSONResponse({"error": "Already exists"}, status_code=409)

    if is_dir:
        target.mkdir(parents=True, exist_ok=True)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")

    return {"status": "ok", "path": rel_path, "type": "dir" if is_dir else "file"}


@app.delete("/api/agent/{agent_id}/file")
async def delete_file(agent_id: str, path: str):
    """Delete a file or directory."""
    agent_dir = AGENTS_DIR / agent_id
    if not agent_dir.exists():
        return JSONResponse({"error": "Agent not found"}, status_code=404)

    if not path or ".." in path or path.startswith("/"):
        return JSONResponse({"error": "Invalid path"}, status_code=400)

    target = (agent_dir / path).resolve()
    if not str(target).startswith(str(agent_dir.resolve())):
        return JSONResponse({"error": "Path outside workspace"}, status_code=403)
    if not target.exists():
        return JSONResponse({"error": "Not found"}, status_code=404)

    if target.is_dir():
        import shutil
        shutil.rmtree(target)
    else:
        target.unlink()
    return {"status": "ok"}


@app.post("/api/agent/{agent_id}/file/rename")
async def rename_file(agent_id: str, data: dict):
    """Rename a file or directory."""
    agent_dir = AGENTS_DIR / agent_id
    if not agent_dir.exists():
        return JSONResponse({"error": "Agent not found"}, status_code=404)

    old_path = data.get("path", "").strip()
    new_name = data.get("new_name", "").strip()

    if not old_path or not new_name or ".." in old_path or ".." in new_name:
        return JSONResponse({"error": "Invalid path"}, status_code=400)

    old_target = (agent_dir / old_path).resolve()
    if not str(old_target).startswith(str(agent_dir.resolve())):
        return JSONResponse({"error": "Path outside workspace"}, status_code=403)
    if not old_target.exists():
        return JSONResponse({"error": "Not found"}, status_code=404)

    new_target = old_target.parent / new_name
    if new_target.exists():
        return JSONResponse({"error": "Target already exists"}, status_code=409)

    old_target.rename(new_target)
    return {"status": "ok"}


# Memory API: vector search + file fallback
@app.get("/api/agent/{agent_id}/memory/search")
async def search_memory(agent_id: str, q: str):
    # Primary: vector search via MemoryManager
    vector_results = []
    try:
        memories = await orchestrator.memory.search(q, agent_id=agent_id, top_k=10)
        for m in memories:
            vector_results.append({
                "type": "vector",
                "source": m.get("source", "unknown"),
                "content": m.get("content", ""),
                "distance": m.get("distance"),
                "created_at": m.get("created_at"),
            })
    except Exception as e:
        logger.warning("vector_memory_search_failed", error=str(e))

    # Fallback: file-based keyword search
    agent_dir = AGENTS_DIR / agent_id
    file_results = []
    for root, _, filenames in os.walk(agent_dir):
        for fname in filenames:
            if fname.endswith(".md") or fname.endswith(".jsonl"):
                fpath = Path(root) / fname
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        content = f.read()
                    if q.lower() in content.lower():
                        lines = content.splitlines()
                        snippet = ""
                        for i, line in enumerate(lines):
                            if q.lower() in line.lower():
                                start = max(0, i - 1)
                                end = min(len(lines), i + 2)
                                snippet = "\n".join(lines[start:end])
                                break
                        file_results.append({
                            "type": "file",
                            "file": fpath.relative_to(agent_dir).as_posix(),
                            "snippet": snippet,
                        })
                except Exception:
                    pass
    return {"vector_results": vector_results, "file_results": file_results[:10]}


# Task API
@app.get("/api/agent/{agent_id}/tasks")
async def get_tasks(agent_id: str):
    path = AGENTS_DIR / agent_id / "workspace" / "tasks.json"
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


@app.post("/api/agent/{agent_id}/tasks")
async def update_tasks(agent_id: str, request: Request):
    data = await request.json()
    workspace = AGENTS_DIR / agent_id / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    path = workspace / "tasks.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return {"status": "ok"}


# Tool Execution API (for direct tool calls from UI)
@app.post("/api/agent/{agent_id}/tools/{tool_name}")
async def execute_tool(agent_id: str, tool_name: str, request: Request):
    data = await request.json()
    try:
        result = await orchestrator.tools.execute(tool_name, data)
        return {"result": result}
    except Exception as e:
        logger.error("tool_execution_api_failed", tool=tool_name, error=str(e))
        return JSONResponse({"error": str(e)}, status_code=500)


# Evolution Status API
@app.get("/api/evolution/status")
async def evolution_status():
    genes_dir = BASE_DIR / "shared" / "genes"
    skills_dir = BASE_DIR / "shared" / "skills"
    genes = []
    skills = []
    if genes_dir.exists():
        for f in sorted(genes_dir.glob("gene_*.py")):
            try:
                content = f.read_text(encoding="utf-8")
                name = f.stem
                desc = ""
                for line in content.splitlines()[:10]:
                    if line.strip().startswith('"""') and len(line.strip()) > 3:
                        desc = line.strip().strip('"').strip()
                        break
                genes.append({"name": name, "description": desc or "Gene", "filename": f.name})
            except Exception:
                pass
    if skills_dir.exists():
        for f in sorted(skills_dir.glob("skill_*.py")):
            try:
                content = f.read_text(encoding="utf-8")
                name = f.stem
                desc = ""
                for line in content.splitlines()[:10]:
                    if '"description"' in line or "'description'" in line:
                        parts = line.split(':', 1)
                        if len(parts) == 2:
                            desc = parts[1].strip().strip('",\'').strip('"').strip("'")
                            break
                skills.append({"name": name, "description": desc or "Skill", "filename": f.name})
            except Exception:
                pass
    logs = []
    log_dir = BASE_DIR / "logs"
    if log_dir.exists():
        for f in sorted(log_dir.glob("*.log"), reverse=True)[:20]:
            try:
                logs.append({"name": f.name, "content": f.read_text(encoding="utf-8")[-4000:]})
            except Exception:
                pass
    return {
        "enabled": config.evolution.get("enabled", True),
        "gene_count": len(genes),
        "skill_count": len(skills),
        "genes": genes,
        "skills": skills,
        "scheduler_running": evo_scheduler.running if hasattr(evo_scheduler, "running") else False,
        "logs": logs,
    }


# Evolution Entity Content API
@app.get("/api/evolution/entity/{entity_type}/{name}")
async def get_evolution_entity(entity_type: str, name: str):
    if entity_type not in ("gene", "skill"):
        return JSONResponse({"error": "Invalid entity type"}, status_code=400)
    target_dir = BASE_DIR / "shared" / ("genes" if entity_type == "gene" else "skills")
    target_file = target_dir / f"{name}.py"
    if not target_file.exists():
        return JSONResponse({"error": "Not found"}, status_code=404)
    try:
        content = target_file.read_text(encoding="utf-8")
        return {"name": name, "type": entity_type, "content": content}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# Daemon Config API
@app.get("/api/config")
async def get_daemon_config():
    return {
        "llm": config.llm,
        "evolution": config.evolution,
        "memory": config.memory,
        "tools": config.tools,
        "gateway": config.gateway,
        "mcp_servers": config.mcp_servers,
        "integrations": config.integrations,
    }


@app.post("/api/config")
async def update_daemon_config(data: dict):
    path = BASE_DIR / "daemon" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    # Hot-reload: update in-memory config and rebuild LLM clients
    for key in ("llm", "evolution", "memory", "tools", "gateway", "mcp_servers", "integrations"):
        if key in data:
            setattr(config, key, data[key])
    # Rebuild all LLM clients (including plugins) from new config
    try:
        orchestrator.llm.rebuild_clients()
        logger.info("llm_clients_reloaded", providers=list(orchestrator.llm.clients.keys()))
    except Exception as e:
        logger.error("llm_reload_failed", error=str(e))

    return {"status": "ok"}


# Tools log API
@app.get("/api/tools/logs")
async def get_tools_logs():
    logs = []
    log_dir = BASE_DIR / "logs"
    if log_dir.exists():
        for f in sorted(log_dir.glob("tools*.log"), reverse=True)[:10]:
            try:
                logs.append({"name": f.name, "content": f.read_text(encoding="utf-8")[-8000:]})
            except Exception:
                pass
    return {"logs": logs}


# ── Multi-Agent Orchestration APIs ─────────────────────────────────────────

@app.get("/api/agents")
async def list_agents():
    """List all active agents and their status."""
    agents = []
    for agent_id, agent in orchestrator.agents.items():
        agents.append({
            "agent_id": agent_id,
            "status": "busy" if agent.pending_question else "idle",
            "plan_mode": agent.plan_mode,
            "max_turns": agent.max_turns,
        })
    # Also list agent directories
    if AGENTS_DIR.exists():
        for agent_dir in AGENTS_DIR.iterdir():
            if agent_dir.is_dir():
                aid = agent_dir.name
                if not any(a["agent_id"] == aid for a in agents):
                    agents.append({"agent_id": aid, "status": "offline", "plan_mode": False})
    return {"agents": agents}


@app.get("/api/teams")
async def list_teams():
    """List all teams."""
    return {"teams": orchestrator._teams}


@app.post("/api/teams")
async def create_team(request: Request):
    data = await request.json()
    name = data.get("name", "")
    if not name:
        return JSONResponse({"error": "Team name is required"}, status_code=400)
    if name in orchestrator._teams:
        return JSONResponse({"error": "Team already exists"}, status_code=409)
    orchestrator.create_team(name)
    return {"status": "ok", "team": name, "agents": []}


@app.delete("/api/teams/{team_name}")
async def delete_team(team_name: str):
    if team_name not in orchestrator._teams:
        return JSONResponse({"error": "Team not found"}, status_code=404)
    del orchestrator._teams[team_name]
    return {"status": "ok"}


# ── Integration Webhook Endpoints ──────────────────────────────────────────────

@app.post("/api/integrations/feishu/webhook")
async def feishu_webhook(request: Request):
    """Receive events from Feishu Open Platform (HTTPS callback).

    Configure this URL in Feishu app settings as the callback URL.
    Example: https://your-domain.com/api/integrations/feishu/webhook
    """
    try:
        body = await request.json()
        feishu = integration_manager.get("feishu")
        if feishu and feishu.is_running:
            # Forward to Feishu SDK's WebSocket handler if using HTTP callback mode
            logger.info("feishu_webhook_received", event=body.get("header", {}).get("event_type", ""))
        return JSONResponse({"code": 0})
    except Exception as e:
        logger.error("feishu_webhook_error", error=str(e))
        return JSONResponse({"code": 1, "msg": str(e)}, status_code=500)


@app.post("/api/integrations/qq/webhook")
async def qq_webhook(request: Request):
    """Receive events from QQ Guild (QQ频道) via webhook mode.

    Configure this URL in QQ Open Platform as the event callback URL.
    Example: https://your-domain.com/api/integrations/qq/webhook
    """
    try:
        body = await request.json()
        qq = integration_manager.get("qq")
        if qq and qq.is_running:
            headers = dict(request.headers)
            await qq.handle_webhook(body, headers)
        return JSONResponse({"code": 0})
    except Exception as e:
        logger.error("qq_webhook_error", error=str(e))
        return JSONResponse({"code": 1, "msg": str(e)}, status_code=500)


@app.get("/api/integrations/wechat/webhook")
async def wechat_webhook_verify(request: Request):
    """Handle WeChat Work URL verification GET challenge."""
    try:
        qq = integration_manager.get("wechat")
        if not (qq and qq.is_running):
            return PlainTextResponse("not enabled", status_code=503)

        params = dict(request.query_params)
        # WeChat Work verification: return echostr decoded
        result = await qq.handle_webhook(b"", {}, params)
        if result.get("status") == 200 and "body" in result:
            return PlainTextResponse(result["body"])
        return PlainTextResponse("verify failed", status_code=400)
    except Exception as e:
        logger.error("wechat_verify_error", error=str(e))
        return PlainTextResponse("error", status_code=500)


@app.post("/api/integrations/wechat/webhook")
async def wechat_webhook(request: Request):
    """Receive events from WeChat Work application (HTTPS callback).

    Configure this URL in WeChat Work app settings as the callback URL.
    Example: https://your-domain.com/api/integrations/wechat/webhook
    """
    try:
        body = await request.body()
        params = dict(request.query_params)
        wechat = integration_manager.get("wechat")
        if wechat and wechat.is_running:
            result = await wechat.handle_webhook(body, dict(request.headers), params)
            if result.get("status") == 200:
                return PlainTextResponse(result.get("body", "success"))
            return PlainTextResponse(result.get("body", "error"), status_code=result.get("status", 500))
        return PlainTextResponse("not enabled", status_code=503)
    except Exception as e:
        logger.error("wechat_webhook_error", error=str(e))
        return PlainTextResponse("error", status_code=500)


@app.get("/api/integrations/status")
async def integrations_status():
    """Return status of all integrations."""
    return JSONResponse(integration_manager.status)

@app.post("/api/teams/{team_name}/agents/{agent_id}")
async def add_agent_to_team(team_name: str, agent_id: str):
    if team_name not in orchestrator._teams:
        return JSONResponse({"error": "Team not found"}, status_code=404)
    if agent_id in orchestrator._teams[team_name]:
        return JSONResponse({"error": "Agent already in team"}, status_code=409)
    orchestrator._teams[team_name].append(agent_id)
    return {"status": "ok", "team": team_name, "agents": orchestrator._teams[team_name]}


@app.delete("/api/teams/{team_name}/agents/{agent_id}")
async def remove_agent_from_team(team_name: str, agent_id: str):
    if team_name not in orchestrator._teams:
        return JSONResponse({"error": "Team not found"}, status_code=404)
    if agent_id not in orchestrator._teams[team_name]:
        return JSONResponse({"error": "Agent not in team"}, status_code=404)
    orchestrator._teams[team_name].remove(agent_id)
    return {"status": "ok", "team": team_name, "agents": orchestrator._teams[team_name]}


@app.post("/api/agents/{agent_id}/delegate")
async def delegate_to_agent(agent_id: str, request: Request):
    """Delegate a task to a specific agent."""
    data = await request.json()
    task = data.get("task", "")
    if not task:
        return JSONResponse({"error": "Task is required"}, status_code=400)
    try:
        result = await orchestrator.delegate(agent_id, task)
        return {"status": "ok", "agent_id": agent_id, "result": result}
    except Exception as e:
        logger.error("delegate_agent_failed", agent_id=agent_id, error=str(e))
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/teams/{team_name}/delegate")
async def delegate_to_team(team_name: str, request: Request):
    """Delegate a task to all agents in a team, running in parallel."""
    data = await request.json()
    task = data.get("task", "")
    parallel = data.get("parallel", True)
    merge_strategy = data.get("merge", "concat")
    if not task:
        return JSONResponse({"error": "Task is required"}, status_code=400)
    if team_name not in orchestrator._teams:
        return JSONResponse({"error": "Team not found"}, status_code=404)
    try:
        results = await orchestrator.run_team(team_name, task, parallel=parallel)
        merged = await orchestrator.merge_results(results, strategy=merge_strategy)
        return {"status": "ok", "team": team_name, "results": results, "merged": merged}
    except Exception as e:
        logger.error("team_delegate_failed", team=team_name, error=str(e))
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Integrations APIs ──────────────────────────────────────────────────────

@app.get("/api/integrations")
async def list_integrations():
    """Return status of all integrations."""
    return {"integrations": integration_manager.status}


@app.get("/api/integrations/{name}")
async def get_integration(name: str):
    """Get config + status for a single integration."""
    status = integration_manager.status
    if name not in status:
        return JSONResponse({"error": "Unknown integration"}, status_code=404)
    cfg = config.integrations.get(name, {})
    # Strip secrets from response
    safe_cfg = {k: ("***" if "token" in k or "key" in k else v) for k, v in cfg.items()}
    return {"name": name, "config": safe_cfg, "status": status[name]}


@app.post("/api/integrations/{name}")
async def update_integration(name: str, request: Request):
    """Update integration config and optionally restart it."""
    data = await request.json()
    if name not in IntegrationManager.available_integrations():
        return JSONResponse({"error": "Unknown integration"}, status_code=404)
    # Persist to daemon config
    config.integrations[name] = data
    path = BASE_DIR / "daemon" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    full = {
        "llm": config.llm,
        "evolution": config.evolution,
        "memory": config.memory,
        "tools": config.tools,
        "gateway": config.gateway,
        "mcp_servers": config.mcp_servers,
        "integrations": config.integrations,
    }
    with open(path, "w", encoding="utf-8") as f:
        import json as _json
        _json.dump(full, f, indent=2, ensure_ascii=False)
    # Hot-restart: stop old instance, create + start new one
    old = integration_manager.get(name)
    if old:
        try:
            await old.disconnect()
        except Exception:
            pass
    integration_manager._config[name] = data
    integration_manager._integrations.pop(name, None)
    if data.get("enabled", False):
        from xmclaw.integrations import manager as _mgr
        cls = _mgr._REGISTRY.get(name)
        if cls:
            inst = cls(data)
            inst.on_message(integration_manager._make_handler(name, data.get("agent_id", "default")))
            integration_manager._integrations[name] = inst
            try:
                await inst.connect()
            except Exception as e:
                logger.error("integration_restart_failed", name=name, error=str(e))
    return {"status": "ok", "name": name}


@app.post("/api/integrations/{name}/send")
async def send_via_integration(name: str, request: Request):
    """Send a message via a named integration (for testing)."""
    data = await request.json()
    text = data.get("text", "")
    target = data.get("target")
    inst = integration_manager.get(name)
    if not inst:
        return JSONResponse({"error": f"Integration '{name}' not running"}, status_code=404)
    try:
        await inst.send(text, target=target)
        return {"status": "ok"}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/events")
async def get_events(event_type: str | None = None, source: str | None = None, limit: int = 50):
    """Get recent events from the EventBus history."""
    bus = get_event_bus()
    events = bus.get_history(event_type=event_type, source=source, limit=limit)
    return {"events": [e.to_dict() for e in events]}


@app.get("/api/events/stats")
async def get_event_stats():
    """Get event bus statistics."""
    bus = get_event_bus()
    return bus.get_stats()


@app.get("/api/agent/{agent_id}/sessions")
async def list_sessions(agent_id: str):
    """List conversation sessions for an agent."""
    from xmclaw.utils.paths import get_agent_dir
    agent_dir = get_agent_dir(agent_id)
    if agent_dir is None:
        return {"sessions": []}
    sessions_dir = agent_dir / "memory" / "sessions"
    sessions = []
    if sessions_dir.exists():
        for f in sorted(sessions_dir.glob("*.jsonl"), reverse=True)[:50]:
            try:
                lines = f.read_text(encoding="utf-8").strip().splitlines()
                turns = []
                for line in lines[-10:]:
                    try:
                        turns.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
                first_msg = turns[0].get("user", "")[:100] if turns else ""
                sessions.append({
                    "id": f.stem,
                    "file": f.name,
                    "turn_count": len(lines),
                    "preview": first_msg,
                    "modified": f.stat().st_mtime,
                    "recent_turns": turns,
                })
            except Exception:
                pass
    return {"sessions": sessions}


# ── Multimodal / Media APIs ────────────────────────────────────────────────

@app.post("/asr")
async def asr_endpoint(request: Request):
    """Transcribe audio to text using the ASR tool.

    Accepts JSON: {"audio": "<base64 data URI or file path>", "language": "zh"}
    Returns: {"text": "..."}
    """
    data = await request.json()
    audio = data.get("audio", "")
    language = data.get("language", "")
    prompt = data.get("prompt", "")
    if not audio:
        return JSONResponse({"error": "audio field required"}, status_code=400)
    try:
        from xmclaw.tools.asr import ASRTool
        result = await ASRTool().execute(audio=audio, language=language, prompt=prompt)
        return {"text": result}
    except Exception as e:
        logger.error("asr_endpoint_error", error=str(e))
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/tts")
async def tts_endpoint(request: Request):
    """Convert text to speech.

    Accepts JSON: {"text": "...", "voice": "alloy", "speed": 1.0}
    Returns: {"audio_uri": "data:audio/mp3;base64,...", "file": "/tmp/..."}
    """
    data = await request.json()
    text = data.get("text", "")
    voice = data.get("voice", "alloy")
    speed = float(data.get("speed", 1.0))
    if not text:
        return JSONResponse({"error": "text field required"}, status_code=400)
    try:
        from xmclaw.tools.tts import TTSTool
        result = await TTSTool().execute(text=text, voice=voice, speed=speed)
        # Parse result: "[TTS OK] File: /tmp/xxx\ndata:audio/mp3;base64,..."
        lines = result.split("\n", 2)
        file_path = ""
        audio_uri = ""
        for line in lines:
            if line.startswith("[TTS OK] File:"):
                file_path = line.replace("[TTS OK] File:", "").strip()
            elif line.startswith("data:audio"):
                audio_uri = line.strip()
        return {"audio_uri": audio_uri, "file": file_path, "raw": result}
    except Exception as e:
        logger.error("tts_endpoint_error", error=str(e))
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/vision")
async def vision_endpoint(request: Request):
    """Analyze an image with a multimodal LLM.

    Accepts JSON: {"image": "<path|URL|data URI>", "prompt": "..."}
    Returns: {"result": "..."}
    """
    data = await request.json()
    image = data.get("image", "")
    prompt = data.get("prompt", "Please describe this image.")
    if not image:
        return JSONResponse({"error": "image field required"}, status_code=400)
    try:
        from xmclaw.tools.vision import VisionTool
        result = await VisionTool().execute(image=image, prompt=prompt)
        return {"result": result}
    except Exception as e:
        logger.error("vision_endpoint_error", error=str(e))
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/media/upload")
async def upload_media(request: Request):
    """Upload a media file (base64 data URI) and return a server-side path.

    Accepts JSON: {"name": "file.png", "data": "data:image/png;base64,..."}
    Returns: {"path": "/media/xxx.png", "url": "/media/xxx.png"}
    """
    import base64 as _b64
    import uuid as _uuid
    data = await request.json()
    name = data.get("name", "upload")
    raw = data.get("data", "")
    if not raw:
        return JSONResponse({"error": "data field required"}, status_code=400)
    try:
        if "," in raw:
            header, b64 = raw.split(",", 1)
        else:
            header, b64 = "", raw
        ext = "bin"
        for fmt in ["png", "jpg", "jpeg", "gif", "webp", "mp3", "wav", "webm", "mp4", "pdf"]:
            if fmt in header or name.lower().endswith(f".{fmt}"):
                ext = fmt
                break
        media_dir = BASE_DIR / "web" / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        uid = _uuid.uuid4().hex[:10]
        fname = f"{uid}_{name}"
        fpath = media_dir / fname
        fpath.write_bytes(_b64.b64decode(b64))
        url = f"/media/{fname}"
        return {"path": str(fpath), "url": url}
    except Exception as e:
        logger.error("media_upload_error", error=str(e))
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/code_exec")
async def code_exec_endpoint(request: Request):
    """Execute code safely in the sandbox.

    Accepts JSON: {"code": "...", "language": "python", "stdin": "", "timeout": 30}
    Returns: {"result": "..."}
    """
    data = await request.json()
    code = data.get("code", "")
    language = data.get("language", "python")
    stdin = data.get("stdin", "")
    timeout = int(data.get("timeout", 30))
    if not code:
        return JSONResponse({"error": "code field required"}, status_code=400)
    try:
        from xmclaw.tools.code_exec import CodeExecTool
        result = await CodeExecTool().execute(code=code, language=language, stdin=stdin, timeout=timeout)
        return {"result": result}
    except Exception as e:
        logger.error("code_exec_endpoint_error", error=str(e))
        return JSONResponse({"error": str(e)}, status_code=500)


@app.websocket("/agent/{agent_id}")
async def agent_websocket(websocket: WebSocket, agent_id: str):
    await websocket.accept()
    logger.info("websocket_connected", agent_id=agent_id)

    # EventBus → WebSocket bridge: forward all events to this client
    bus = get_event_bus()
    sub_id: str | None = None

    # Track pending async generator so ask_user tool calls can be resumed via .asend()
    from typing import Any
    _pending_agent: Any = None

    async def _forward_event(event: Event):
        try:
            await websocket.send_text(json.dumps({
                "type": "event",
                "event": event.to_dict(),
            }, ensure_ascii=False))
        except Exception:
            pass

    sub_id = bus.subscribe_wildcard(_forward_event)

    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            msg_type = message.get("type", "")
            user_input = message.get("content", "")

            # Handle special message types from the frontend
            if msg_type == "file_upload":
                # Save the uploaded file and turn it into a user message
                raw_name = message.get("name", "upload")
                # Sanitize filename: strip path separators to prevent traversal
                file_name = raw_name.replace("/", "_").replace("\\", "_").replace("..", "_")
                file_data = message.get("data", "")  # base64 data URI
                if file_data:
                    try:
                        import base64 as _b64, uuid as _uuid
                        if "," in file_data:
                            header, b64 = file_data.split(",", 1)
                        else:
                            header, b64 = "", file_data
                        ext = "bin"
                        for fmt in ["png", "jpg", "jpeg", "gif", "webp", "mp3", "wav", "webm", "mp4", "pdf"]:
                            if fmt in header or file_name.lower().endswith(f".{fmt}"):
                                ext = fmt
                                break
                        media_dir = BASE_DIR / "web" / "media"
                        media_dir.mkdir(parents=True, exist_ok=True)
                        uid = _uuid.uuid4().hex[:10]
                        fname = f"{uid}_{file_name}"
                        fpath = media_dir / fname
                        fpath.write_bytes(_b64.b64decode(b64))
                        file_url = f"/media/{fname}"
                        # Treat as a user message referencing the file
                        if ext in ("png", "jpg", "jpeg", "gif", "webp"):
                            user_input = f"[图片] {file_url}"
                        else:
                            user_input = f"[文件] {file_url}"
                    except Exception as upload_err:
                        logger.error("ws_file_upload_error", error=str(upload_err))
                        user_input = f"[文件上传失败: {upload_err}]"
                else:
                    continue

            elif msg_type == "voice_input":
                # Transcribe audio using ASRTool and use the text as user input
                audio_data = message.get("audio", "")
                if audio_data:
                    try:
                        from xmclaw.tools.asr import ASRTool
                        transcribed = await ASRTool().execute(audio=audio_data)
                        user_input = transcribed.strip()
                        if not user_input:
                            await websocket.send_text(json.dumps({"type": "error", "content": "语音识别失败，请重试"}))
                            continue
                        # Echo the transcription back so the user can see what was heard
                        await websocket.send_text(json.dumps({"type": "transcription", "text": user_input}))
                    except Exception as asr_err:
                        logger.error("ws_voice_input_error", error=str(asr_err))
                        await websocket.send_text(json.dumps({"type": "error", "content": f"语音识别错误: {asr_err}"}))
                        continue
                else:
                    continue

            elif msg_type == "ask_user_answer":
                # User replied to an ask_user prompt — resume the pending generator via .asend()
                answer = message.get("answer", "")
                if _pending_agent is not None:
                    try:
                        # Inject the answer into the generator (the pending yield receives it)
                        async for chunk in _pending_agent.asend(answer):
                            await websocket.send_text(chunk)
                            # Check if the generator is asking another question (nested ask_user)
                            try:
                                parsed = json.loads(chunk)
                                if parsed.get("type") == "ask_user":
                                    _pending_agent = None
                                    break
                            except (json.JSONDecodeError, TypeError):
                                pass
                        # Generator finished normally
                        _pending_agent = None
                        await websocket.send_text(json.dumps({"type": "done"}))
                    except StopAsyncIteration:
                        _pending_agent = None
                        await websocket.send_text(json.dumps({"type": "done"}))
                    except Exception as e:
                        _pending_agent = None
                        logger.error("asend_error", error=str(e))
                        await websocket.send_text(json.dumps({"type": "error", "content": str(e)}))
                else:
                    # No pending generator — treat as a fresh message
                    user_input = f"[RESUME]{answer}"
                continue

            # Normal message: run the agent and stream chunks
            agen = orchestrator.run_agent(agent_id, user_input)
            try:
                async for chunk in agen:
                    await websocket.send_text(chunk)
                    # Detect ask_user pause: if the chunk is ask_user, the generator
                    # is now waiting for .asend(answer). Keep agen alive as _pending_agent.
                    try:
                        parsed = json.loads(chunk)
                        if parsed.get("type") == "ask_user":
                            _pending_agent = agen
                            break  # exit async for; agen is paused, waiting for .asend()
                    except (json.JSONDecodeError, TypeError):
                        pass

                # Generator finished normally (did not hit ask_user)
                if _pending_agent is None:
                    await websocket.send_text(json.dumps({"type": "done"}))
                # else: ask_user paused — _pending_agent keeps agen alive

            except StopAsyncIteration:
                # Generator exhausted
                _pending_agent = None
                await websocket.send_text(json.dumps({"type": "done"}))
            except Exception as e:
                _pending_agent = None
                import traceback
                logger.error("agent_run_error", agent_id=agent_id, error=str(e), tb=traceback.format_exc())
                await websocket.send_text(json.dumps({"type": "error", "content": str(e)}))
    except Exception as e:
        import traceback
        logger.error("websocket_error", agent_id=agent_id, error=str(e), traceback=traceback.format_exc())
        try:
            await websocket.send_text(json.dumps({"type": "error", "content": str(e)}))
            await websocket.close()
        except Exception:
            pass
    finally:
        if sub_id:
            bus.unsubscribe(sub_id)
        logger.info("websocket_disconnected", agent_id=agent_id)


def main():
    import uvicorn
    host = config.gateway["host"]
    port = config.gateway["port"]
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()