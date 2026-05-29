"""MemoryGraph — 记忆图谱系统。

在现有 sqlite-vec 旁提供关系型记忆存储。
节点类型: event / entity / state / intent
边类型: CAUSED_BY / RELATED_TO / LEADS_TO / CONTRADICTS / PART_OF
"""
from __future__ import annotations

import asyncio
import sqlite3
import sqlite_vec
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

NodeType = Literal["event", "entity", "state", "intent"]
EdgeType = Literal["CAUSED_BY", "RELATED_TO", "LEADS_TO", "CONTRADICTS", "PART_OF"]

# Patch A (2026-05-10): no longer captured at module-import time.
# graph.db path resolved lazily via paths.default_graph_db_path() in
# MemoryGraph.__init__ so XMC_DATA_DIR / XMC_V2_GRAPH_DB_PATH overrides
# actually reroute. Pre-Patch-A this baked Path.home() at import,
# defeating the unified-paths anti-req.


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
    """记忆图谱 — 关系型记忆的存储与查询。

    Phase B 升级：原生 sqlite-vec 加速相似度检索；当扩展不可用时
    透明回退到手动 cosine 计算。
    """

    _DEFAULT_DIM = 1536  # OpenAI text-embedding-3-small

    def __init__(
        self,
        db_path: Path | str | None = None,
        *,
        bus: Any | None = None,
        embedding_dim: int | None = None,
    ) -> None:
        if db_path is None:
            from xmclaw.utils.paths import default_graph_db_path
            db_path = default_graph_db_path()
        self.db_path = str(db_path)
        self._bus = bus
        self._embedding_dim = embedding_dim
        self._vec_supported = False
        self._vec_dim: int | None = None
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
        # Attempt to load sqlite-vec extension for vector KNN.
        try:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            self._vec_supported = True
        except (AttributeError, ImportError, sqlite3.OperationalError):
            self._vec_supported = False
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

        # Phase B: create sqlite-vec virtual table when extension is available.
        if self._vec_supported:
            self._ensure_vec_table()

    def _ensure_vec_table(self) -> None:
        """Idempotently create the graph_nodes_vec vec0 table."""
        dim = self._embedding_dim
        if dim is None:
            # Infer from existing nodes, or fall back to default.
            cur = self._conn.cursor()
            row = cur.execute(
                "SELECT embedding FROM graph_nodes WHERE embedding IS NOT NULL LIMIT 1"
            ).fetchone()
            if row and row["embedding"]:
                dim = len(row["embedding"]) // 4
            else:
                dim = self._DEFAULT_DIM
        self._vec_dim = dim
        cur = self._conn.cursor()
        # Check if table already exists with matching dimension.
        try:
            existing = cur.execute(
                "SELECT type FROM sqlite_schema WHERE name = 'graph_nodes_vec'"
            ).fetchone()
        except sqlite3.OperationalError:
            existing = None
        if existing:
            # Verify dimension compatibility by probing a dummy query.
            try:
                dummy = b"\x00" * (dim * 4)
                cur.execute(
                    "SELECT distance FROM graph_nodes_vec WHERE embedding MATCH ? AND k = 1",
                    (dummy,),
                )
                return  # Table exists and dimension matches.
            except sqlite3.OperationalError:
                # Dimension mismatch — drop and recreate.
                cur.execute("DROP TABLE graph_nodes_vec")
                self._conn.commit()
        try:
            cur.execute(
                f"CREATE VIRTUAL TABLE graph_nodes_vec USING vec0("
                f"node_id TEXT PRIMARY KEY, embedding float[{dim}]"
                f")"
            )
            self._conn.commit()
        except sqlite3.OperationalError:
            self._vec_supported = False

    # ── 写操作 ──

    async def add_node(self, node: GraphNode) -> str:
        """添加节点。同步维护 vec 表。"""
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
            # Sync vec table when sqlite-vec is available.
            if self._vec_supported and embedding_blob and self._vec_dim:
                try:
                    cur.execute(
                        "INSERT OR REPLACE INTO graph_nodes_vec (node_id, embedding) VALUES (?, ?)",
                        (node.id, embedding_blob),
                    )
                except sqlite3.OperationalError:
                    pass  # vec table may not exist yet; ignore.
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
            if self._vec_supported:
                try:
                    cur.execute("DELETE FROM graph_nodes_vec WHERE node_id = ?", (node_id,))
                except sqlite3.OperationalError:
                    pass
            self._conn.commit()

    async def remove_edge(self, edge_id: str) -> None:
        async with self._get_write_lock():
            cur = self._conn.cursor()
            cur.execute("DELETE FROM graph_edges WHERE id = ?", (edge_id,))
            self._conn.commit()

    # ── 读操作 ──

    async def get_node(self, node_id: str) -> GraphNode | None:
        def _run():
            cur = self._conn.cursor()
            return cur.execute("SELECT * FROM graph_nodes WHERE id = ?", (node_id,)).fetchone()
        row = await asyncio.to_thread(_run)
        return self._row_to_node(row) if row else None

    def _get_neighbors_sync(
        self,
        node_id: str,
        *,
        relation: EdgeType | None = None,
        depth: int = 1,
        min_strength: float = 0.0,
    ) -> list[tuple[GraphEdge, GraphNode]]:
        """Synchronous variant for use inside threaded callers."""
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

    async def get_neighbors(
        self,
        node_id: str,
        *,
        relation: EdgeType | None = None,
        depth: int = 1,
        min_strength: float = 0.0,
    ) -> list[tuple[GraphEdge, GraphNode]]:
        """获取节点的邻居（支持多跳）。"""
        return await asyncio.to_thread(
            self._get_neighbors_sync,
            node_id,
            relation=relation,
            depth=depth,
            min_strength=min_strength,
        )

    def _find_path_sync(
        self,
        source_id: str,
        target_id: str,
        *,
        max_depth: int = 5,
    ) -> list[GraphEdge] | None:
        """Synchronous BFS variant."""
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

    async def find_path(
        self,
        source_id: str,
        target_id: str,
        *,
        max_depth: int = 5,
    ) -> list[GraphEdge] | None:
        """查找两节点之间的最短路径（BFS）。"""
        return await asyncio.to_thread(
            self._find_path_sync, source_id, target_id, max_depth=max_depth,
        )

    def _query_by_type_sync(
        self,
        type: NodeType,  # noqa: A002
        *,
        limit: int = 10,
    ) -> list[GraphNode]:
        """Synchronous variant."""
        cur = self._conn.cursor()
        rows = cur.execute(
            "SELECT * FROM graph_nodes WHERE type = ? ORDER BY created_at DESC LIMIT ?",
            (type, limit),
        ).fetchall()
        return [self._row_to_node(r) for r in rows]

    async def query_by_type(
        self,
        type: NodeType,  # noqa: A002
        *,
        limit: int = 10,
    ) -> list[GraphNode]:
        """按类型查询节点。"""
        return await asyncio.to_thread(self._query_by_type_sync, type, limit=limit)

    def _query_by_time_range_sync(
        self,
        since: float | None = None,
        until: float | None = None,
        *,
        type: NodeType | None = None,  # noqa: A002
        limit: int = 50,
    ) -> list[GraphNode]:
        """Synchronous temporal-index variant."""
        cur = self._conn.cursor()
        clauses: list[str] = []
        params: list[Any] = []
        if since is not None:
            clauses.append("created_at >= ?")
            params.append(float(since))
        if until is not None:
            clauses.append("created_at <= ?")
            params.append(float(until))
        if type is not None:
            clauses.append("type = ?")
            params.append(type)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(int(limit))
        rows = cur.execute(
            f"SELECT * FROM graph_nodes{where} "
            f"ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [self._row_to_node(r) for r in rows]

    async def query_by_time_range(
        self,
        since: float | None = None,
        until: float | None = None,
        *,
        type: NodeType | None = None,  # noqa: A002
        limit: int = 50,
    ) -> list[GraphNode]:
        """``xmclaw-architecture-redesign.md §3.3.2`` temporal-index API."""
        return await asyncio.to_thread(
            self._query_by_time_range_sync,
            since, until, type=type, limit=limit,
        )

    # ── 主动回忆 ──

    async def proactive_recall(
        self,
        context: str,
        *,
        intent_embedding: list[float] | None = None,
        limit: int = 5,
        neighbor_depth: int = 2,
        time_window_hours: float = 168,
    ) -> str:
        """基于当前上下文，多策略主动推送相关历史记忆。

        召回策略（按优先级）：
        1. **Intent 向量相似** — 找相似 intent，沿 LEADS_TO/RELATED_TO
           多跳扩展取 event/entity。
        2. **上下文实体匹配** — 从 context 提取关键词，匹配 entity 节点，
           取关联 event。
        3. **时间近邻** — 取最近 ``time_window_hours`` 内的 event 节点。

        结果按综合分数排序（相关性 + 边强度 + 时间衰减），去重后返回。
        """
        if not context.strip():
            return ""

        # 2026-05-29 perf fix: proactive_recall 内部大量同步 sqlite3
        # 查询会阻塞 event loop。把整个计算放到后台线程。
        def _run():
            now = time.time()
            since = now - (time_window_hours * 3600)
            scored: dict[str, tuple[GraphNode, float]] = {}  # id → (node, score)

            # ── Strategy 1: intent vector similarity + multi-hop expansion ──
            intent_nodes: list[tuple[GraphNode, float]] = []
            if intent_embedding:
                intent_nodes = self._find_similar_node_raw_sync(
                    intent_embedding, "intent", k=3,
                )
            # Fallback: 无 embedding 时取最近 intent 作为种子
            if not intent_nodes:
                recent_intents = self._query_by_type_sync("intent", limit=3)
                intent_nodes = [(n, 0.0) for n in recent_intents]

            for intent_node, intent_dist in intent_nodes:
                # 多跳邻居扩展（LEADS_TO / RELATED_TO）
                neighbors = self._get_neighbors_sync(
                    intent_node.id,
                    depth=neighbor_depth,
                    min_strength=0.3,
                )
                for edge, node in neighbors:
                    if node.type not in ("event", "entity", "state"):
                        continue
                    # 综合分数 = 相关性 + 边强度 + 时间衰减
                    recency = max(0.0, 1.0 - (now - node.created_at) / (time_window_hours * 3600))
                    score = (1.0 - intent_dist) * 0.4 + edge.strength * 0.3 + recency * 0.3
                    if node.id in scored:
                        # 同一节点多路径到达，取最高分
                        old_node, old_score = scored[node.id]
                        if score > old_score:
                            scored[node.id] = (node, score)
                    else:
                        scored[node.id] = (node, score)

            # ── Strategy 2: entity keyword match ──
            # 简单启发：把 context 按空白切分为候选词，长度>2 的当作关键词
            keywords = {w.strip(".,!?;:\"'()[]{}「」『』") for w in context.split() if len(w) > 2}
            if keywords:
                cur = self._conn.cursor()
                # 用 LIKE 做前缀匹配（SQLite 无全文索引时的轻量回退）
                rows = cur.execute(
                    "SELECT * FROM graph_nodes WHERE type = 'entity' AND ("
                    + " OR ".join("content LIKE ?" for _ in keywords)
                    + ")",
                    [f"%{kw}%" for kw in keywords],
                ).fetchall()
                for row in rows:
                    node = self._row_to_node(row)
                    recency = max(0.0, 1.0 - (now - node.created_at) / (time_window_hours * 3600))
                    score = 0.5 + recency * 0.3  # 关键词命中给中等基础分
                    if node.id in scored:
                        _old_node, old_score = scored[node.id]
                        scored[node.id] = (node, max(score, old_score))
                    else:
                        scored[node.id] = (node, score)
                    # 再扩展一步取关联 event
                    ent_neighbors = self._get_neighbors_sync(
                        node.id, depth=1, min_strength=0.2,
                    )
                    for edge, nbr in ent_neighbors:
                        if nbr.type != "event":
                            continue
                        recency = max(0.0, 1.0 - (now - nbr.created_at) / (time_window_hours * 3600))
                        nbr_score = edge.strength * 0.4 + recency * 0.3
                        if nbr.id in scored:
                            _old_node, old_score = scored[nbr.id]
                            scored[nbr.id] = (nbr, max(nbr_score, old_score))
                        else:
                            scored[nbr.id] = (nbr, nbr_score)

            # ── Strategy 3: temporal recency ──
            recent_events = self._query_by_time_range_sync(
                since=since, type="event", limit=limit * 2,
            )
            for node in recent_events:
                if node.id in scored:
                    continue
                recency = max(0.0, 1.0 - (now - node.created_at) / (time_window_hours * 3600))
                scored[node.id] = (node, recency * 0.5)

            if not scored:
                return ""

            # 排序并格式化
            ranked = sorted(scored.values(), key=lambda x: x[1], reverse=True)
            lines = ["💡 相关历史记忆:"]
            for i, (node, _score) in enumerate(ranked[:limit], 1):
                prefix = {"event": "📌", "entity": "🏷", "state": "📊", "intent": "🎯"}.get(
                    node.type, "•"
                )
                lines.append(f"   {i}. {prefix} {node.content}")
            return "\n".join(lines)

        return await asyncio.to_thread(_run)

    # ── 维护 ──

    async def prune_orphaned(self) -> int:
        """删除无连接的孤立节点。"""
        def _run():
            cur = self._conn.cursor()
            cur.execute("""
                DELETE FROM graph_nodes
                WHERE id NOT IN (SELECT source_id FROM graph_edges)
                  AND id NOT IN (SELECT target_id FROM graph_edges)
            """)
            count = cur.rowcount
            self._conn.commit()
            return count
        return await asyncio.to_thread(_run)

    async def stats(self) -> dict[str, Any]:
        def _run():
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
        return await asyncio.to_thread(_run)

    def close(self) -> None:
        self._conn.close()

    # ── helpers ──

    async def _find_similar_node(
        self,
        embedding: list[float],
        type: NodeType,  # noqa: A002
        threshold: float,
    ) -> str | None:
        """用 L2 distance 找最近邻；返回最近的一个节点 id（若 distance
        在 threshold 内），否则 None。
        """
        candidates = await self._find_similar_node_raw(embedding, type, k=1)
        if not candidates:
            return None
        _node, distance = candidates[0]
        if distance <= threshold:
            return _node.id
        return None

    def _find_similar_node_raw_sync(
        self,
        embedding: list[float],
        type: NodeType,  # noqa: A002
        k: int,
    ) -> list[tuple[GraphNode, float]]:
        """Synchronous variant."""
        import struct
        if self._vec_supported and self._vec_dim and len(embedding) == self._vec_dim:
            qblob = struct.pack(f"{len(embedding)}f", *embedding)
            cur = self._conn.cursor()
            try:
                rows = cur.execute(
                    "SELECT n.id, n.type, n.content, n.embedding, n.created_at, "
                    "       n.memory_item_id, v.distance "
                    "FROM graph_nodes_vec v "
                    "JOIN graph_nodes n ON n.id = v.node_id "
                    "WHERE v.embedding MATCH ? AND k = ? AND n.type = ? "
                    "ORDER BY v.distance",
                    (qblob, k, type),
                ).fetchall()
                return [
                    (self._row_to_node(r), float(r["distance"]))
                    for r in rows
                    if r["distance"] is not None
                ]
            except sqlite3.OperationalError:
                pass
        return self._manual_similarity_search_sync(embedding, type, k)

    async def _find_similar_node_raw(
        self,
        embedding: list[float],
        type: NodeType,  # noqa: A002
        k: int,
    ) -> list[tuple[GraphNode, float]]:
        """找相似节点，返回 [(node, distance), ...] 按 distance 升序。"""
        return await asyncio.to_thread(
            self._find_similar_node_raw_sync, embedding, type, k,
        )

    def _manual_similarity_search_sync(
        self,
        embedding: list[float],
        type: NodeType,  # noqa: A002
        k: int,
    ) -> list[tuple[GraphNode, float]]:
        """纯 Python cosine-distance 回退（O(n) 扫描）——同步版本。"""
        q_norm = self._l2_norm(embedding)
        if q_norm == 0:
            return []
        cur = self._conn.cursor()
        rows = cur.execute(
            "SELECT * FROM graph_nodes WHERE type = ? AND embedding IS NOT NULL",
            (type,),
        ).fetchall()
        scored: list[tuple[GraphNode, float]] = []
        for row in rows:
            node = self._row_to_node(row)
            if not node.embedding:
                continue
            d_norm = self._l2_norm(list(node.embedding))
            if d_norm == 0:
                continue
            sim = sum(a * b for a, b in zip(embedding, node.embedding)) / (q_norm * d_norm)
            sim = max(-1.0, min(1.0, sim))
            scored.append((node, 1.0 - sim))
        scored.sort(key=lambda x: x[1])
        return scored[:k]

    async def _manual_similarity_search(
        self,
        embedding: list[float],
        type: NodeType,  # noqa: A002
        k: int,
    ) -> list[tuple[GraphNode, float]]:
        """纯 Python cosine-distance 回退（O(n) 扫描）。"""
        return await asyncio.to_thread(
            self._manual_similarity_search_sync, embedding, type, k,
        )

    @staticmethod
    def _l2_norm(vec: list[float]) -> float:
        import math
        return math.sqrt(sum(v * v for v in vec))

    async def _update_node_content(self, node_id: str, content: str) -> None:
        def _run():
            cur = self._conn.cursor()
            cur.execute(
                "UPDATE graph_nodes SET content = ? WHERE id = ?",
                (content, node_id),
            )
            self._conn.commit()
        return await asyncio.to_thread(_run)

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
