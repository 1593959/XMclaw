"""Vector memory store using sqlite-vec."""
import json
import sqlite3
from pathlib import Path
from typing import Any

from xmclaw.llm.router import LLMRouter
from xmclaw.utils.log import logger


def _load_sqlite_vec(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("SELECT vec_version()")
        return True
    except sqlite3.OperationalError:
        pass
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        conn.load_extension(sqlite_vec.loadable_path())
        return True
    except Exception as e:
        logger.warning("sqlite_vec_load_failed", error=str(e))
        return False


class VectorStore:
    def __init__(self, db_path: Path, llm_router: LLMRouter | None = None):
        self.db_path = db_path
        self.llm = llm_router
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_vec()

    def _init_vec(self) -> None:
        if not _load_sqlite_vec(self.conn):
            logger.warning("sqlite_vec_not_loaded")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                source TEXT,
                content TEXT,
                metadata TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        try:
            self.conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_vectors USING vec0(
                    memory_id INTEGER PRIMARY KEY,
                    embedding FLOAT[1024] distance_metric=cosine
                )
            """)
        except sqlite3.OperationalError as e:
            logger.warning("vec0_table_creation_failed", error=str(e))
        self.conn.commit()

    async def add(self, agent_id: str, content: str, source: str = "unknown", metadata: dict | None = None) -> int:
        cursor = self.conn.execute(
            "INSERT INTO memories (agent_id, source, content, metadata) VALUES (?, ?, ?, ?)",
            (agent_id, source, content, json.dumps(metadata or {}, ensure_ascii=False)),
        )
        memory_id = cursor.lastrowid
        self.conn.commit()

        embedding = await self._embed(content)
        if embedding:
            try:
                self.conn.execute(
                    "INSERT INTO memory_vectors (memory_id, embedding) VALUES (?, ?)",
                    (memory_id, self._serialize(embedding)),
                )
                self.conn.commit()
            except Exception as e:
                logger.warning("vector_insert_failed", error=str(e))
        return memory_id

    async def search(self, query: str, agent_id: str | None = None, top_k: int = 5) -> list[dict[str, Any]]:
        embedding = await self._embed(query)
        if not embedding:
            return []
        try:
            sql = """
                SELECT m.id, m.agent_id, m.source, m.content, m.metadata, m.created_at,
                       distance
                FROM memory_vectors v
                JOIN memories m ON v.memory_id = m.id
                WHERE v.embedding MATCH ? AND k = ?
            """
            params = [self._serialize(embedding), top_k]
            if agent_id:
                sql += " AND m.agent_id = ?"
                params.append(agent_id)
            sql += " ORDER BY distance"
            cursor = self.conn.execute(sql, params)
            rows = cursor.fetchall()
            return [
                {
                    "id": row["id"],
                    "agent_id": row["agent_id"],
                    "source": row["source"],
                    "content": row["content"],
                    "metadata": json.loads(row["metadata"] or "{}"),
                    "created_at": row["created_at"],
                    "distance": row["distance"],
                }
                for row in rows
            ]
        except Exception as e:
            logger.warning("vector_search_failed", error=str(e))
            return []

    async def _embed(self, text: str) -> list[float] | None:
        if not self.llm:
            return None
        try:
            vectors = await self.llm.embed([text])
            if vectors and vectors[0]:
                return vectors[0]
        except Exception as e:
            logger.warning("embed_failed", error=str(e))
        return None

    def _serialize(self, vector: list[float]) -> bytes:
        import struct
        return struct.pack(f"{len(vector)}f", *vector)

    def close(self) -> None:
        self.conn.close()
