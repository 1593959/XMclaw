"""FastAPI + WebSocket server for XMclaw daemon."""
import asyncio
import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket
from xmclaw.daemon.config import DaemonConfig
from xmclaw.core.orchestrator import AgentOrchestrator
from xmclaw.evolution.scheduler import EvolutionScheduler
from xmclaw.daemon.static import mount_static_files
from xmclaw.utils.log import logger

config = DaemonConfig.load()
orchestrator = AgentOrchestrator()
evo_scheduler = EvolutionScheduler()


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


@app.websocket("/agent/{agent_id}")
async def agent_websocket(websocket: WebSocket, agent_id: str):
    await websocket.accept()
    logger.info("websocket_connected", agent_id=agent_id)

    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            user_input = message.get("content", "")

            async for chunk in orchestrstrator.run_agent(agent_id, user_input):
                await websocket.send_text(json.dumps({"type": "chunk", "content": chunk}))

            await websocket.send_text(json.dumps({"type": "done"}))
    except Exception as e:
        logger.error("websocket_error", agent_id=agent_id, error=str(e))
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
