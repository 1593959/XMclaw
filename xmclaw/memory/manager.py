"""Unified memory manager.

Layers:
- SQLite: structured metadata, agent configs, todo states
- JSONL: raw session logs
- ChromaDB: vector embeddings for insights and skills
- Markdown: core long-term memory (MEMORY.md, PROFILE.md, etc.)
"""
import asyncio
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from xmclaw.memory.sqlite_store import SQLiteStore
from xmclaw.memory.session_manager import SessionManager
from xmclaw.memory.vector_store import VectorStore
from xmclaw.llm.router import LLMRouter
from xmclaw.core.event_bus import Event, EventType, get_event_bus
from xmclaw.utils.paths import BASE_DIR, get_agent_dir
from xmclaw.utils.log import logger


class MemoryManager:
    def __init__(self, llm_router: LLMRouter | None = None):
        self.sqlite: SQLiteStore | None = None
        self.sessions: SessionManager | None = None
        self.vectors: VectorStore | None = None
        self.llm = llm_router
        self._event_bus = get_event_bus()
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized:
            return
        db_path = BASE_DIR / "shared" / "memory.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.sqlite = SQLiteStore(db_path)
        self.sessions = SessionManager(get_agent_dir("default") / "memory" / "sessions")
        self.vectors = VectorStore(db_path, llm_router=self.llm)
        self._initialized = True
        logger.info("memory_manager_initialized")

    async def close(self) -> None:
        if self.vectors:
            self.vectors.close()
        if self.sqlite:
            self.sqlite.close()
        self._initialized = False

    async def load_context(self, agent_id: str, user_input: str) -> dict[str, Any]:
        """Load context for prompt building."""
        history = await self.sessions.get_recent(agent_id, limit=10) if self.sessions else []
        memories = await self.search(user_input, agent_id=agent_id, top_k=5) if self.vectors else []
        # Load recent reflection insights so the agent can act on past lessons immediately
        insights = self.get_insights(agent_id, limit=10) if self.sqlite else []
        return {
            "history": history,
            "memories": memories,
            "insights": insights,
            "tool_descriptions": "",  # Filled by orchestrator
        }

    async def save_turn(self, agent_id: str, user_input: str, response: str, tool_calls: list[dict]) -> None:
        """Save a conversation turn."""
        if self.sessions:
            await self.sessions.append(agent_id, {
                "timestamp": datetime.now().isoformat(),
                "user": user_input,
                "assistant": response,
                "tool_calls": tool_calls,
            })
        # Fire-and-forget vector indexing — do NOT await so the embedding HTTP
        # call does not block MEMORY_UPDATED from reaching the frontend immediately.
        if self.vectors:
            asyncio.create_task(self.vectors.add(
                agent_id, f"User: {user_input}\nAgent: {response}", source="turn"
            ))

        await self._event_bus.publish(Event(
            event_type=EventType.MEMORY_UPDATED,
            source=agent_id,
            payload={"action": "save_turn", "preview": user_input[:200]},
        ))

    async def search(self, query: str, agent_id: str | None = None, top_k: int = 5) -> list[dict]:
        """Search long-term memory via vector store."""
        if not self.vectors:
            return []
        return await self.vectors.search(query, agent_id=agent_id, top_k=top_k)

    async def add_memory(self, agent_id: str, content: str, source: str = "manual", metadata: dict | None = None) -> int:
        """Explicitly add a memory entry (fire-and-forget vector indexing)."""
        if not self.vectors:
            return -1
        asyncio.create_task(self.vectors.add(agent_id, content, source=source, metadata=metadata))
        return 0

    def save_insight(self, agent_id: str, insight: dict) -> None:
        """Save an insight to SQLite."""
        if self.sqlite:
            self.sqlite.insert_insight(agent_id, insight)

    def get_insights(self, agent_id: str, limit: int = 50) -> list[dict]:
        """Retrieve recent insights."""
        if self.sqlite:
            return self.sqlite.get_insights(agent_id, limit)
        return []
