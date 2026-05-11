"""GraphExtractor — 从对话文本中抽取图谱节点与边。

Phase 5: 自动图构建。使用轻量级启发式 + 可选 LLM 增强：
- 意图检测：用户消息中的动词短语
- 实体提取：大写词组、引号内容、文件路径
- 事件提取：助手消息中的动作描述
- 关系推断：时序先后、因果关系

设计决策：默认纯启发式（零成本），当 embedder 可用时
启用语义合并避免重复节点。
"""
from __future__ import annotations

import re
import time

from xmclaw.utils.log import get_logger

import uuid
from dataclasses import dataclass

from xmclaw.cognition.memory_graph import GraphEdge, MemoryGraph

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ExtractedTriple:
    """抽取的三元组。"""

    subject: str
    relation: str
    object: str  # noqa: A003
    confidence: float = 1.0


class GraphExtractor:
    """从对话文本中抽取图谱结构。"""

    # 意图动词（中文 + 英文）
    _INTENT_VERBS = frozenset({
        "想", "要", "需要", "希望", "打算", "计划", "准备",
        "请", "帮", "给", "做", "写", "读", "改", "删", "查",
        "创建", "修改", "删除", "查看", "搜索", "提交", "部署",
        "want", "need", "help", "create", "modify", "delete",
        "search", "find", "write", "read", "update", "deploy",
    })

    # 动作描述模式（助手消息中）
    _ACTION_PATTERNS = [
        re.compile(r"我(已经|将|会|正在|刚|先)?(.{2,30}?)(了|过|完)?[。，]"),
        re.compile(r"(完成|创建|修改|删除|添加|写入|读取|搜索|找到|提交|部署)(.{1,30}?)[。，]"),
    ]

    # 实体模式
    _ENTITY_PATTERNS = [
        re.compile(r'"([^"]{2,50})"'),  # 引号内容
        re.compile(r"'([^']{2,50})'"),  # 单引号内容
        re.compile(r"`([^`]{2,50})`"),  # 反引号内容
        re.compile(r"[A-Z][a-zA-Z0-9_]*(?:\.[a-zA-Z0-9_]+)+"),  # 文件路径 / 模块名
        re.compile(r"[\w\-]+\.(py|js|ts|json|md|yaml|yml|toml|txt|csv)"),  # 文件名
    ]

    def __init__(self, graph: MemoryGraph | None = None) -> None:
        self._graph = graph

    # ── 公共 API ──

    async def extract_from_turn(
        self,
        *,
        session_id: str,
        user_content: str,
        assistant_content: str,
    ) -> list[ExtractedTriple]:
        """从一轮对话中抽取三元组并写入图谱。"""
        if self._graph is None:
            return []

        triples: list[ExtractedTriple] = []

        # 1. 抽取意图节点（来自用户消息）
        intents = self._extract_intents(user_content)
        intent_nodes: dict[str, str] = {}  # text -> node_id
        for intent_text in intents:
            nid, _ = await self._graph.merge_node(
                content=intent_text,
                type="intent",
            )
            intent_nodes[intent_text] = nid

        # 2. 抽取事件节点（来自助手消息）
        events = self._extract_events(assistant_content)
        event_nodes: dict[str, str] = {}
        for event_text in events:
            nid, _ = await self._graph.merge_node(
                content=event_text,
                type="event",
            )
            event_nodes[event_text] = nid

        # 3. 抽取实体节点
        entities = self._extract_entities(user_content + "\n" + assistant_content)
        entity_nodes: dict[str, str] = {}
        for ent_text in entities:
            nid, _ = await self._graph.merge_node(
                content=ent_text,
                type="entity",
            )
            entity_nodes[ent_text] = nid

        # 4. 建立边关系
        # intent -> event (LEADS_TO)
        for itext, iid in intent_nodes.items():
            for etext, eid in event_nodes.items():
                await self._add_edge(iid, eid, "LEADS_TO", 0.6)
                triples.append(ExtractedTriple(itext, "LEADS_TO", etext, 0.6))

        # intent -> entity (RELATED_TO)
        for itext, iid in intent_nodes.items():
            for etext, eid in entity_nodes.items():
                if self._mentions(itext, etext) or self._mentions(etext, itext):
                    await self._add_edge(iid, eid, "RELATED_TO", 0.5)
                    triples.append(ExtractedTriple(itext, "RELATED_TO", etext, 0.5))

        # event -> entity (RELATED_TO)
        for etext, eid in event_nodes.items():
            for ent_text, ent_id in entity_nodes.items():
                if self._mentions(etext, ent_text):
                    await self._add_edge(eid, ent_id, "RELATED_TO", 0.5)
                    triples.append(ExtractedTriple(etext, "RELATED_TO", ent_text, 0.5))

        # event -> event (时序先后，PART_OF)
        event_ids = list(event_nodes.values())
        for i in range(len(event_ids) - 1):
            await self._add_edge(event_ids[i], event_ids[i + 1], "PART_OF", 0.4)

        # 5. 创建会话级聚合节点
        if event_nodes:
            session_node_id, _ = await self._graph.merge_node(
                content=f"session:{session_id}",
                type="state",
            )
            for eid in event_nodes.values():
                await self._add_edge(session_node_id, eid, "PART_OF", 0.3)

        return triples

    # ── 内部启发式 ──

    def _extract_intents(self, text: str) -> list[str]:
        """从用户消息中抽取意图。"""
        results: list[str] = []
        # 按句子分割
        for sent in self._split_sentences(text):
            sent = sent.strip()
            if not sent:
                continue
            # 如果句子包含意图动词，整句作为一个意图
            if any(v in sent for v in self._INTENT_VERBS):
                # 截断过长的句子
                if len(sent) > 100:
                    sent = sent[:100] + "…"
                results.append(sent)
                continue
            # 否则，尝试提取动词 + 宾语
            match = re.search(r"[\u4e00-\u9fa5]{0,3}[\u4e00-\u9fa5]*[^\u4e00-\u9fa5]*", sent)
            if match and len(match.group()) >= 4:
                results.append(match.group()[:80])
        # 去重
        seen: set[str] = set()
        unique: list[str] = []
        for r in results:
            if r not in seen:
                seen.add(r)
                unique.append(r)
        return unique[:5]  # 每轮最多 5 个意图

    def _extract_events(self, text: str) -> list[str]:
        """从助手消息中抽取事件。"""
        results: list[str] = []
        for pat in self._ACTION_PATTERNS:
            for m in pat.finditer(text):
                # 取整个匹配或第二个捕获组
                evt = m.group(2) if m.lastindex and m.lastindex >= 2 else m.group(0)
                if evt and len(evt) >= 3:
                    evt = evt.strip(" 。，！？")
                    if len(evt) > 100:
                        evt = evt[:100] + "…"
                    results.append(evt)
        # 去重
        seen: set[str] = set()
        unique: list[str] = []
        for r in results:
            if r not in seen:
                seen.add(r)
                unique.append(r)
        return unique[:5]

    def _extract_entities(self, text: str) -> list[str]:
        """抽取实体。"""
        results: list[str] = []
        for pat in self._ENTITY_PATTERNS:
            for m in pat.finditer(text):
                ent = m.group(1) if m.lastindex else m.group(0)
                if ent and len(ent) >= 2:
                    # 截断
                    if len(ent) > 60:
                        ent = ent[:60] + "…"
                    results.append(ent)
        # 去重
        seen: set[str] = set()
        unique: list[str] = []
        for r in results:
            if r not in seen:
                seen.add(r)
                unique.append(r)
        return unique[:10]

    def _split_sentences(self, text: str) -> list[str]:
        """按句子分割。"""
        # 中文句号 + 英文句号 + 换行
        parts = re.split(r'[。！？\n;]+', text)
        return [p.strip() for p in parts if p.strip()]

    def _mentions(self, text_a: str, text_b: str) -> bool:
        """判断 text_a 是否提及 text_b（简单包含）。"""
        a_lower = text_a.lower()
        b_lower = text_b.lower()
        return b_lower in a_lower

    async def _add_edge(
        self,
        source_id: str,
        target_id: str,
        relation: str,
        strength: float,
    ) -> None:
        """添加边（忽略重复）。"""
        try:
            await self._graph.add_edge(
                GraphEdge(
                    id=uuid.uuid4().hex,
                    source_id=source_id,
                    target_id=target_id,
                    relation=relation,  # type: ignore[arg-type]
                    strength=strength,
                    created_at=time.time(),
                )
            )
        except Exception:
            log.warning("graph_extractor.duplicate_edge", exc_info=True)
