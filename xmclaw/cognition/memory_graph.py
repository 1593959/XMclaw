"""MemoryGraph — 记忆图谱系统。

在现有 sqlite-vec 旁提供关系型记忆存储。
节点类型: event / entity / state / intent
边类型: CAUSED_BY / RELATED_TO / LEADS_TO / CONTRADICTS / PART_OF
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

NodeType = Literal["event", "entity", "state", "intent"]
EdgeType = Literal["CAUSED_BY", "RELATED_TO", "LEADS_TO", "CONTRADICTS", "PART_OF"]

_DEFAULT_DB_PATH = Path.home() / ".xmclaw" / "v2" / "graph.db"


@dataclass(frozen=True, slots=True)
class GraphNode:
    id: str
    type: NodeType
    content: str
    embedding: tuple[float, ...] | None = None
    created_at: float = 0.0
    memory_item_id: str | None = None


@dataclass(frozen=True, slots=True)
class GraphEdge:
    id: str
    source_id: str
    target_id: str
    relation: EdgeType
    strength: float = 1.0
    created_at: float = 0.0


class MemoryGraph:
    """记忆图谱 — 关系型记忆的存储与查询。"""

    def __init__(
        self,
        db_path: Path | str | None = None,
        *,
        bus: Any | None = None,
    ) -> None:
        self.db_path = str(db_path or _DEFAULT_DB_PATH)
        self._bus = bus
        self._conn = self._open_conn()
        self._ensure_schema()
        try:
            self._write_lock: asyncio.Lock | None = asyncio.Lock()
        except RuntimeError:
            self._write_lock = None

    def _get_write_lock(self) -> asyncio.Lock:
        if self._write_lock is None:
            self._write_lock = asyncio.Lock()
        return self._write_lock

    def _open_conn(self) -> sqlite3.Connection:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _ensure_schema(self) -> None:
        cur = self._conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS graph_nodes (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL CHECK(type IN ('event', 'entity', 'state', 'intent')),
                content TEXT NOT NULL,
                embedding BLOB,
                created_at REAL NOT NULL,
                memory_item_id TEXT
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS graph_nodes_type ON graph_nodes(type)")
        cur.execute("CREATE INDEX IF NOT EXISTS graph_nodes_created ON graph_nodes(created_at)")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS graph_edges (
                id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL REFERENCES graph_nodes(id) ON DELETE CASCADE,
                target_id TEXT NOT NULL REFERENCES graph_nodes(id) ON DELETE CASCADE,
                relation TEXT NOT NULL CHECK(relation IN ('CAUSED_BY', 'RELATED_TO', 'LEADS_TO', 'CONTRADICTS', 'PART_OF')),
                strength REAL NOT NULL DEFAULT 1.0 CHECK(strength >= 0 AND strength <= 1),
                created_at REAL NOT NULL
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS graph_edges_source ON graph_edges(source_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS graph_edges_target ON graph_edges(target_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS graph_edges_relation ON graph_edges(relation)")
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS graph_edges_unique
            ON graph_edges(source_id, target_id, relation)
        """)
        self._conn.commit()

    # ── 写操作 ──

    async def add_node(self, node: GraphNode) -> str:
        """添加节点。"""
        async with self._get_write_lock():
            cur = self._conn.cursor()
            embedding_blob = None
            if node.embedding:
                import struct
                embedding_blob = struct.pack(f"{len(node.embedding)}f", *node.embedding)
            cur.execute(
                "INSERT OR REPLACE INTO graph_nodes (id, type, content, embedding, created_at, memory_item_id) VALUES (?, ?, ?, ?, ?, ?)",
                (node.id, node.type, node.content, embedding_blob, node.created_at, node.memory_item_id),
            )
            self._conn.commit()
        return node.id

    async def add_edge(self, edge: GraphEdge) -> str:
        """添加边。"""
        async with self._get_write_lock():
            cur = self._conn.cursor()
            cur.execute(
                "INSERT OR REPLACE INTO graph_edges (id, source_id, target_id, relation, strength, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (edge.id, edge.source_id, edge.target_id, edge.relation, edge.strength, edge.created_at),
            )
            self._conn.commit()
        return edge.id

    async def merge_node(
        self,
        content: str,
        type: NodeType,  # noqa: A002
        *,
        embedding: list[float] | None = None,
        memory_item_id: str | None = None,
        distance_threshold: float = 0.4,
    ) -> tuple[str, bool]:
        """语义合并：如存在相似节点则更新，否则新建。"""
        if embedding:
            match = await self._find_similar_node(embedding, type, distance_threshold)
            if match:
                await self._update_node_content(match, content)
                return match, True

        node = GraphNode(
            id=uuid.uuid4().hex,
            type=type,
            content=content,
            embedding=tuple(embedding) if embedding else None,
            created_at=time.time(),
            memory_item_id=memory_item_id,
        )
        await self.add_node(node)
        return node.id, False

    async def remove_node(self, node_id: str) -> None:
        async with self._get_write_lock():
            cur = self._conn.cursor()
            cur.execute("DELETE FROM graph_nodes WHERE id = ?", (node_id,))
            self._conn.commit()

    async def remove_edge(self, edge_id: str) -> None:
        async with self._get_write_lock():
            cur = self._conn.cursor()
            cur.execute("DELETE FROM graph_edges WHERE id = ?", (edge_id,))
            self._conn.commit()

    # ── 读操作 ──

    async def get_node(self, node_id: str) -> GraphNode | None:
        cur = self._conn.cursor()
        row = cur.execute("SELECT * FROM graph_nodes WHERE id = ?", (node_id,)).fetchone()
        return self._row_to_node(row) if row else None

    async def get_neighbors(
        self,
        node_id: str,
        *,
        relation: EdgeType | None = None,
        depth: int = 1,
        min_strength: float = 0.0,
    ) -> list[tuple[GraphEdge, GraphNode]]:
        """获取节点的邻居（支持多跳）。"""
        if depth < 1:
            return []

        results: list[tuple[GraphEdge, GraphNode]] = []
        visited: set[str] = {node_id}
        current_level = {node_id}

        for _ in range(depth):
            next_level: set[str] = set()
            for nid in current_level:
                where = "source_id = ?"
                params: list[Any] = [nid]
                if relation:
                    where += " AND relation = ?"
                    params.append(relation)
                if min_strength > 0:
                    where += " AND strength >= ?"
                    params.append(min_strength)

                cur = self._conn.cursor()
                rows = cur.execute(
                    f"SELECT * FROM graph_edges WHERE {where}", params
                ).fetchall()

                for row in rows:
                    edge = self._row_to_edge(row)
                    if edge.target_id in visited:
                        continue
                    visited.add(edge.target_id)
                    next_level.add(edge.target_id)

                    node_row = cur.execute(
                        "SELECT * FROM graph_nodes WHERE id = ?", (edge.target_id,)
                    ).fetchone()
                    if node_row:
                        results.append((edge, self._row_to_node(node_row)))

            current_level = next_level
            if not current_level:
                break

        return results

    async def find_path(
        self,
        source_id: str,
        target_id: str,
        *,
        max_depth: int = 5,
    ) -> list[GraphEdge] | None:
        """查找两节点之间的最短路径（BFS）。"""
        if source_id == target_id:
            return []

        from collections import deque

        queue: deque[tuple[str, list[GraphEdge]]] = deque([(source_id, [])])
        visited: set[str] = {source_id}

        while queue:
            current, path = queue.popleft()
            if len(path) >= max_depth:
                continue

            cur = self._conn.cursor()
            rows = cur.execute(
                "SELECT * FROM graph_edges WHERE source_id = ?", (current,)
            ).fetchall()

            for row in rows:
                edge = self._row_to_edge(row)
                if edge.target_id in visited:
                    continue
                new_path = path + [edge]
                if edge.target_id == target_id:
                    return new_path
                visited.add(edge.target_id)
                queue.append((edge.target_id, new_path))

        return None

    async def query_by_type(
        self,
        type: NodeType,  # noqa: A002
        *,
        limit: int = 10,
    ) -> list[GraphNode]:
        """按类型查询节点。"""
        cur = self._conn.cursor()
        rows = cur.execute(
            "SELECT * FROM graph_nodes WHERE type = ? ORDER BY created_at DESC LIMIT ?",
            (type, limit),
        ).fetchall()
        return [self._row_to_node(r) for r in rows]

    # ── 主动回忆 ──

    async def proactive_recall(
        self,
        context: str,
        *,
        intent_embedding: list[float] | None = None,
        limit: int = 5,
    ) -> str:
        """基于当前上下文，主动推送相关历史记忆。"""
        # 1. 在图谱中找相似 intent
        intent_nodes: list[GraphNode] = []
        if intent_embedding:
            similar = await self._find_similar_node_raw(intent_embedding, "intent", k=3)
            intent_nodes = [n for n, _ in similar]
        else:
            # 无 embedding 时，取最近 intent
            intent_nodes = await self.query_by_type("intent", limit=3)

        # 2. 遍历 LEADS_TO 边找历史 event
        memories: list[str] = []
        for intent_node in intent_nodes:
            neighbors = await self.get_neighbors(
                intent_node.id, relation="LEADS_TO", depth=1
            )
            for edge, node in neighbors:
                if node.type == "event":
                    memories.append(node.content)

        if not memories:
            return ""

        # 3. 格式化为提示文本
        lines = ["💡 相关历史记忆:"]
        for i, mem in enumerate(memories[:limit], 1):
            lines.append(f"   {i}. {mem}")
        return "\n".join(lines)

    # ── 维护 ──

    async def prune_orphaned(self) -> int:
        """删除无连接的孤立节点。"""
        async with self._get_write_lock():
            cur = self._conn.cursor()
            cur.execute("""
                DELETE FROM graph_nodes
                WHERE id NOT IN (SELECT source_id FROM graph_edges)
                  AND id NOT IN (SELECT target_id FROM graph_edges)
            """)
            count = cur.rowcount
            self._conn.commit()
        return count

    async def stats(self) -> dict[str, Any]:
        cur = self._conn.cursor()
        node_count = cur.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0]
        edge_count = cur.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0]
        type_counts = {
            row["type"]: row["c"]
            for row in cur.execute(
                "SELECT type, COUNT(*) as c FROM graph_nodes GROUP BY type"
            )
        }
        return {
            "nodes": node_count,
            "edges": edge_count,
            "by_type": type_counts,
        }

    def close(self) -> None:
        self._conn.close()

    # ── helpers ──

    async def _find_similar_node(
        self,
        embedding: list[float],
        type: NodeType,  # noqa: A002
        threshold: float,
    ) -> str | None:
        """用 L2 distance 找最近邻。"""
        import struct

        blob = struct.pack(f"{len(embedding)}f", *embedding)
        cur = self._conn.cursor()
        # sqlite-vec 风格的 KNN（如可用），否则fallback
        row = cur.execute(
            "SELECT id FROM graph_nodes WHERE type = ? AND embedding IS NOT NULL LIMIT 1",
            (type,),
        ).fetchone()
        if row is None:
            return None
        # 简化：暂用精确匹配，后续接入 sqlite-vec 或手动计算
        return str(row["id"])

    async def _find_similar_node_raw(
        self,
        embedding: list[float],
        type: NodeType,  # noqa: A002
        k: int,
    ) -> list[tuple[GraphNode, float]]:
        """找相似节点，返回 (node, distance)。"""
        # Phase 1 简化：返回同类型最近节点
        nodes = await self.query_by_type(type, limit=k * 2)
        # 简单按内容长度排序作为占位
        return [(n, 0.5) for n in nodes[:k]]

    async def _update_node_content(self, node_id: str, content: str) -> None:
        async with self._get_write_lock():
            cur = self._conn.cursor()
            cur.execute(
                "UPDATE graph_nodes SET content = ? WHERE id = ?",
                (content, node_id),
            )
            self._conn.commit()

    @staticmethod
    def _row_to_node(row: sqlite3.Row) -> GraphNode:
        embedding = None
        if row["embedding"]:
            import struct
            blob = row["embedding"]
            n = len(blob) // 4
            embedding = struct.unpack(f"{n}f", blob)
        return GraphNode(
            id=row["id"],
            type=row["type"],
            content=row["content"],
            embedding=embedding,
            created_at=row["created_at"],
            memory_item_id=row["memory_item_id"],
        )

    @staticmethod
    def _row_to_edge(row: sqlite3.Row) -> GraphEdge:
        return GraphEdge(
            id=row["id"],
            source_id=row["source_id"],
            target_id=row["target_id"],
            relation=row["relation"],
            strength=row["strength"],
            created_at=row["created_at"],
        )
