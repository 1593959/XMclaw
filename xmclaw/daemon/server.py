"""FastAPI + WebSocket server for XMclaw daemon."""
import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import JSONResponse
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
evo_scheduler = EvolutionScheduler()
integration_manager = IntegrationManager(config.integrations)

AGENTS_DIR = BASE_DIR / "agents"


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("daemon_starting")
    await orchestrator.initialize()
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
@app.get("/api/agent/{agent_id}/files")
async def list_files(agent_id: str):
    agent_dir = AGENTS_DIR / agent_id
    if not agent_dir.exists():
        return JSONResponse({"error": "Agent not found"}, status_code=404)

    files = []
    for root, _, filenames in os.walk(agent_dir):
        for fname in filenames:
            fpath = Path(root) / fname
            rel = fpath.relative_to(agent_dir).as_posix()
            files.append({
                "path": rel,
                "size": fpath.stat().st_size,
            })
    return {"files": sorted(files, key=lambda x: x["path"])}


@app.get("/api/agent/{agent_id}/file")
async def read_file(agent_id: str, path: str):
    agent_dir = AGENTS_DIR / agent_id
    target = (agent_dir / path).resolve()
    # Security: prevent path traversal
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
    """Delegate a task to all agents in a team."""
    data = await request.json()
    task = data.get("task", "")
    if not task:
        return JSONResponse({"error": "Task is required"}, status_code=400)
    if team_name not in orchestrator._teams:
        return JSONResponse({"error": "Team not found"}, status_code=404)
    agents = orchestrator._teams[team_name]
    if not agents:
        return JSONResponse({"error": "Team has no agents"}, status_code=400)
    results = {}
    for agent_id in agents:
        try:
            results[agent_id] = await orchestrator.delegate(agent_id, task)
        except Exception as e:
            results[agent_id] = {"error": str(e)}
    return {"status": "ok", "team": team_name, "results": results}


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


@app.websocket("/agent/{agent_id}")
async def agent_websocket(websocket: WebSocket, agent_id: str):
    await websocket.accept()
    logger.info("websocket_connected", agent_id=agent_id)

    # EventBus → WebSocket bridge: forward all events to this client
    bus = get_event_bus()
    sub_id: str | None = None

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
            user_input = message.get("content", "")

            async for chunk in orchestrator.run_agent(agent_id, user_input):
                try:
                    parsed = json.loads(chunk)
                    await websocket.send_text(chunk)
                    if parsed.get("type") == "ask_user":
                        pass
                except json.JSONDecodeError:
                    await websocket.send_text(json.dumps({"type": "chunk", "content": chunk}))

            # Only send done if the loop actually finished (not ask_user pause)
            agent = orchestrator.agents.get(agent_id)
            if agent and agent.pending_question:
                continue
            await websocket.send_text(json.dumps({"type": "done"}))
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


# ASR (语音转文字) 端点
@app.post("/asr")
async def asr(request: Request):
    try:
        body = await request.json()
        audio_data = body.get("audio", "")
        
        # 使用 SpeechRecognition 库
        import speech_recognition as sr
        import base64
        import tempfile
        import os
        
        r = sr.Recognizer()
        
        # 解码 base64 音频并保存为临时文件
        if "," in audio_data:
            audio_data = audio_data.split(",")[1]
        audio_bytes = base64.b64decode(audio_data)
        
        with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as f:
            f.write(audio_bytes)
            temp_path = f.name
        
        try:
            with sr.AudioFile(temp_path) as source:
                audio = r.record(source)
            
            # 尝试 Google 在线识别（需要网络）
            try:
                text = r.recognize_google(audio, language="zh-CN")
                return {"text": text, "success": True}
            except Exception:
                pass
            
            # 尝试 Sphinx 离线识别（需要安装 PocketSphinx）
            try:
                text = r.recognize_sphinx(audio, language="zh-CN")
                return {"text": text, "success": True}
            except Exception:
                pass
            
            return {"text": "", "success": False, "error": "无法识别语音，请安装 PocketSphinx 或检查网络连接"}
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
    except Exception as e:
        logger.error("asr_error", error=str(e))
        return {"text": "", "success": False, "error": str(e)}


if __name__ == "__main__":
    main()