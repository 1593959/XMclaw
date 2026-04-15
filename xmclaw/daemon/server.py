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
from xmclaw.evolution.scheduler import EvolutionScheduler
from xmclaw.daemon.static import mount_static_files
from xmclaw.utils.log import logger
from xmclaw.utils.paths import BASE_DIR

config = DaemonConfig.load()
orchestrator = AgentOrchestrator()
evo_scheduler = EvolutionScheduler()

AGENTS_DIR = BASE_DIR / "agents"


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("daemon_starting")
    await orchestrator.initialize()
    if config.evolution.get("enabled", True):
        await evo_scheduler.start()
    yield
    logger.info("daemon_stopping")
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
async def update_todos(agent_id: str, data: list):
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


# Memory API (placeholder using file-based search for now)
@app.get("/api/agent/{agent_id}/memory/search")
async def search_memory(agent_id: str, q: str):
    agent_dir = AGENTS_DIR / agent_id
    results = []
    # Search in MEMORY.md and session files
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
                        results.append({
                            "file": fpath.relative_to(agent_dir).as_posix(),
                            "snippet": snippet,
                        })
                except Exception:
                    pass
    return {"results": results[:20]}


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


# Evolution Status API
@app.get("/api/evolution/status")
async def evolution_status():
    # Read from evolution engine state if available; fallback to filesystem
    genes_dir = BASE_DIR / "xmclaw" / "genes"
    skills_dir = BASE_DIR / "skills" if (BASE_DIR / "skills").exists() else BASE_DIR / "xmclaw" / "skills"
    gene_count = len(list(genes_dir.glob("*.json"))) if genes_dir.exists() else 0
    skill_count = len([d for d in skills_dir.iterdir() if d.is_dir()]) if skills_dir.exists() else 0
    return {
        "enabled": config.evolution.get("enabled", True),
        "gene_count": gene_count,
        "skill_count": skill_count,
        "scheduler_running": evo_scheduler.running if hasattr(evo_scheduler, "running") else False,
    }


@app.websocket("/agent/{agent_id}")
async def agent_websocket(websocket: WebSocket, agent_id: str):
    await websocket.accept()
    logger.info("websocket_connected", agent_id=agent_id)

    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            user_input = message.get("content", "")

            async for chunk in orchestrator.run_agent(agent_id, user_input):
                try:
                    parsed = json.loads(chunk)
                    await websocket.send_text(chunk)
                    # If ask_user event, pause and wait for next client message
                    if parsed.get("type") == "ask_user":
                        # The loop has already returned; next client message will be the answer
                        pass
                except json.JSONDecodeError:
                    await websocket.send_text(json.dumps({"type": "chunk", "content": chunk}))

            # Only send done if the loop actually finished (not ask_user pause)
            agent = orchestrator.agents.get(agent_id)
            if agent and agent.pending_question:
                # Waiting for user answer; don't send done yet
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
        logger.info("websocket_disconnected", agent_id=agent_id)


def main():
    import uvicorn
    host = config.gateway["host"]
    port = config.gateway["port"]
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()