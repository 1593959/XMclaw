"""Unified memory manager.

Layers:
- SQLite: structured metadata, agent configs, todo states
- JSONL: raw session logs
- ChromaDB: vector embeddings for insights and skills
- Markdown: core long-term memory (MEMORY.md, PROFILE.md, etc.)
"""
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from xmclaw.memory.sqlite_store import SQLiteStore
from xmclaw.memory.session_manager import SessionManager
from xmclaw.utils.paths import BASE_DIR, get_agent_dir
from xmclaw.utils.log import logger


class MemoryManager:
    def __init__(self):
        self.sqlite: SQLiteStore | None = None
        self.sessions: SessionManager | None = None
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized:
            return
        db_path = BASE_DIR / "shared" / "memory.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.sqlite = SQLiteStore(db_path)
        self.sessions = SessionManager(get_agent_dir("default") / "memory" / "sessions")
        self._initialized = True
        logger.info("memory_manager_initialized")

    async def close(self) -> None:
        if self.sqlite:
            self.sqlite.close()
        self._initialized = False

    async def load_context(self, agent_id: str, user_input: str) -> dict[str, Any]:
        """Load context for prompt building."""
        history = await self.sessions.get_recent(agent_id, limit=10) if self.sessions else []
        return {
            "history": history,
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

    async def search(self, query: str, top_k: int = 5) -> list[dict]:
        """Search long-term memory. Placeholder for ChromaDB integration."""
        # TODO: integrate ChromaDB vector search
        return []

    def save_insight(self, agent_id: str, insight: dict) -> None:
        """Save an insight to SQLite."""
        if self.sqlite:
            self.sqlite.insert_insight(agent_id, insight)

    def get_insights(self, agent_id: str, limit: int = 50) -> list[dict]:
        """Retrieve recent insights."""
        if self.sqlite:
            return self.sqlite.get_insights(agent_id, limit)
        return []
