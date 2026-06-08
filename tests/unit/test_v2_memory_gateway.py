"""Unit tests for CognitiveMemoryGateway (Phase 1 + Phase 2)."""
from __future__ import annotations

import pytest

from xmclaw.memory.v2.gateway import (
    CognitiveMemoryGateway,
    _build_think_prompt,
    _cache_key,
    _merge_cfg,
    _parse_think_response,
    _passthrough_digest_from_obs,
)
from xmclaw.memory.v2.gateway_models import CognitiveDigest, Observation
from xmclaw.memory.v2.models import Fact


# ── Fixtures ─────────────────────────────────────────────────────


class FakeMemoryService:
    def __init__(self, facts: list[Fact] | None = None):
        self.facts = facts or []
        self.remember_calls: list[dict] = []
        self.rwd_calls: list[dict] = []

    async def remember(self, text, **kwargs):
        self.remember_calls.append({"text": text, **kwargs})
        from xmclaw.memory.v2.models import Fact
        return Fact(
            id=f"fact:{text}",
            kind=kwargs.get("kind", "lesson"),
            scope=kwargs.get("scope", "project"),
            text=text,
        )

    async def remember_with_decision(self, text, **kwargs):
        self.rwd_calls.append({"text": text, **kwargs})
        from xmclaw.memory.v2.models import Fact
        return {
            "action": "ADD",
            "fact": Fact(
                id=f"fact:{text}",
                kind=kwargs.get("kind", "lesson"),
                scope=kwargs.get("scope", "project"),
                text=text,
            ),
            "reason": "test",
        }

    async def recall(self, query, **kwargs):
        return []


class FakeLLM:
    def __init__(self, response_text: str = ""):
        self.response_text = response_text
        self.calls: list[list] = []

    async def complete(self, messages, tools=None):
        self.calls.append(messages)
        class Resp:
            content = self.response_text
        return Resp()


@pytest.fixture
def fake_svc():
    return FakeMemoryService()


@pytest.fixture
def gateway_no_llm(fake_svc):
    return CognitiveMemoryGateway(
        memory_service=fake_svc,
        llm=None,
        cfg={},
    )


# ── Phase 1: passthrough ingest ──────────────────────────────────


@pytest.mark.asyncio
async def test_ingest_passthrough_no_llm(gateway_no_llm, fake_svc):
    """Without LLM, ingest is a transparent passthrough to remember()."""
    obs = Observation(
        source="user_msg",
        content="用户做电商生意",
        turn_id="s1",
        timestamp=0.0,
        metadata={"kind_hint": "identity", "scope_hint": "user"},
    )
    fact = await gateway_no_llm.ingest(obs)
    assert fact is not None
    assert fact.text == "用户做电商生意"
    # Default path uses remember_with_decision when available.
    assert fake_svc.rwd_calls[0]["text"] == "用户做电商生意"


@pytest.mark.asyncio
async def test_ingest_batch_passthrough(gateway_no_llm, fake_svc):
    """Batch ingest processes each observation independently."""
    observations = [
        Observation(source="user_msg", content="A", turn_id="s1", timestamp=0.0),
        Observation(source="user_msg", content="B", turn_id="s1", timestamp=0.0),
    ]
    results = await gateway_no_llm.ingest_batch(observations)
    assert len(results) == 2
    assert all(r is not None for r in results)
    assert fake_svc.rwd_calls[0]["text"] == "A"
    assert fake_svc.rwd_calls[1]["text"] == "B"


@pytest.mark.asyncio
async def test_ingest_empty_content(gateway_no_llm):
    """Empty content observation is dropped immediately."""
    obs = Observation(source="user_msg", content="", turn_id="s1", timestamp=0.0)
    fact = await gateway_no_llm.ingest(obs)
    assert fact is None


@pytest.mark.asyncio
async def test_ingest_disabled_gateway(fake_svc):
    """When gateway is disabled, ingest returns None."""
    gw = CognitiveMemoryGateway(
        memory_service=fake_svc,
        cfg={"enabled": False},
    )
    obs = Observation(source="user_msg", content="X", turn_id="s1", timestamp=0.0)
    assert await gw.ingest(obs) is None


# ── Phase 2: THINK with LLM ──────────────────────────────────────


@pytest.mark.asyncio
async def test_think_calls_llm_when_enabled(fake_svc):
    """When LLM is wired and think is enabled, _think calls the LLM."""
    fake_llm = FakeLLM(response_text='{"worth_remembering":true,"synthesized_text":"用户偏好中文","reason":"test"}')
    gw = CognitiveMemoryGateway(
        memory_service=fake_svc,
        llm=fake_llm,
        cfg={"think": {"enabled": True}},
    )
    # Use content that does NOT hit Tier-1 keywords so the LLM path is exercised.
    obs = Observation(
        source="user_msg",
        content="沟通时喜欢用中文",
        turn_id="s1",
        timestamp=0.0,
        metadata={"kind_hint": "preference", "scope_hint": "user"},
    )
    digest = await gw._think(obs)
    assert digest.worth_remembering is True
    assert digest.synthesized_text == "用户偏好中文"
    assert len(fake_llm.calls) == 1


@pytest.mark.asyncio
async def test_think_drops_when_worth_remembering_false(fake_svc):
    """When LLM says worth_remembering=false, the observation is dropped."""
    fake_llm = FakeLLM(response_text='{"worth_remembering":false,"synthesized_text":"","reason":"temp_command"}')
    gw = CognitiveMemoryGateway(
        memory_service=fake_svc,
        llm=fake_llm,
        cfg={"think": {"enabled": True}},
    )
    obs = Observation(
        source="user_msg",
        content="帮我改下配置",
        turn_id="s1",
        timestamp=0.0,
    )
    fact = await gw.ingest(obs)
    assert fact is None
    assert len(fake_svc.remember_calls) == 0


@pytest.mark.asyncio
async def test_think_degrades_when_llm_none(fake_svc):
    """When LLM is None, THINK degrades to passthrough."""
    gw = CognitiveMemoryGateway(
        memory_service=fake_svc,
        llm=None,
        cfg={"think": {"enabled": True}},
    )
    obs = Observation(source="user_msg", content="X", turn_id="s1", timestamp=0.0)
    digest = await gw._think(obs)
    assert digest.worth_remembering is True
    assert digest.synthesized_text == "X"
    assert digest.reason == "phase1_passthrough"


@pytest.mark.asyncio
async def test_think_uses_synthesized_text_in_execute(fake_svc):
    """The synthesized text (not raw content) is passed to remember()."""
    fake_llm = FakeLLM(response_text='{"worth_remembering":true,"synthesized_text":"归纳后的文本","reason":"test"}')
    gw = CognitiveMemoryGateway(
        memory_service=fake_svc,
        llm=fake_llm,
        cfg={"think": {"enabled": True}},
    )
    obs = Observation(source="user_msg", content="原始文本", turn_id="s1", timestamp=0.0)
    fact = await gw.ingest(obs)
    assert fact is not None
    # remember_with_decision path uses the synthesized text.
    assert fake_svc.rwd_calls[0]["text"] == "归纳后的文本"


# ── Phase 2: THINK caching ───────────────────────────────────────


@pytest.mark.asyncio
async def test_think_cache_hit(fake_svc):
    """Identical observation within TTL hits cache, skipping LLM."""
    fake_llm = FakeLLM(response_text='{"worth_remembering":true,"synthesized_text":"A","reason":"test"}')
    gw = CognitiveMemoryGateway(
        memory_service=fake_svc,
        llm=fake_llm,
        cfg={"think": {"enabled": True, "cache_ttl_s": 60}},
    )
    obs = Observation(source="user_msg", content="X", turn_id="s1", timestamp=0.0)
    await gw._think(obs)
    await gw._think(obs)  # same obs → cache hit
    assert len(fake_llm.calls) == 1


# ── Phase 2: prompt / parse helpers ──────────────────────────────


def test_build_think_prompt_with_neighbours():
    obs = Observation(source="user_msg", content="测试", turn_id="s1", timestamp=0.0)
    nb = Fact(id="f1", kind="preference", scope="user", text="用户偏好中文")
    prompt = _build_think_prompt(obs, [nb])
    assert "来源: user_msg" in prompt
    assert "内容: 测试" in prompt
    assert "[preference] 用户偏好中文" in prompt


def test_build_think_prompt_no_neighbours():
    obs = Observation(source="user_msg", content="测试", turn_id="s1", timestamp=0.0)
    prompt = _build_think_prompt(obs, [])
    assert "无相关记忆" in prompt


def test_parse_think_response_valid():
    obs = Observation(source="user_msg", content="原始内容在这里", turn_id="s1", timestamp=0.0)
    digest = _parse_think_response(
        '{"worth_remembering":true,"synthesized_text":"用户偏好使用中文进行交流","reason":"r"}',
        obs, [],
    )
    assert digest.worth_remembering is True
    assert digest.synthesized_text == "用户偏好使用中文进行交流"
    assert digest.reason == "r"


def test_parse_think_response_with_markdown_fence():
    obs = Observation(source="user_msg", content="原始", turn_id="s1", timestamp=0.0)
    digest = _parse_think_response(
        '```json\n{"worth_remembering":false,"synthesized_text":"","reason":"drop"}\n```',
        obs, [],
    )
    assert digest.worth_remembering is False


def test_parse_think_response_invalid_json_fallback():
    obs = Observation(source="user_msg", content="原始", turn_id="s1", timestamp=0.0)
    digest = _parse_think_response("not json", obs, [])
    assert digest.worth_remembering is True
    assert digest.synthesized_text == "原始"


def test_parse_think_response_empty_synthesized_fallback():
    obs = Observation(source="user_msg", content="原始", turn_id="s1", timestamp=0.0)
    digest = _parse_think_response(
        '{"worth_remembering":true,"synthesized_text":"","reason":"test"}',
        obs, [],
    )
    assert digest.synthesized_text == "原始"


# ── Misc helpers ─────────────────────────────────────────────────


def test_merge_cfg_deep():
    base = {"think": {"enabled": False, "ttl": 300}}
    over = {"think": {"enabled": True}}
    out = _merge_cfg(base, over)
    assert out["think"]["enabled"] is True
    assert out["think"]["ttl"] == 300


def test_cache_key_deterministic():
    obs1 = Observation(source="a", content="b", turn_id="t", timestamp=0.0, metadata={"kind_hint": "k"})
    obs2 = Observation(source="a", content="b", turn_id="t", timestamp=0.0, metadata={"kind_hint": "k"})
    assert _cache_key(obs1) == _cache_key(obs2)


def test_passthrough_digest_from_obs():
    obs = Observation(
        source="user_msg", content="文本", turn_id="s1", timestamp=0.0,
        metadata={"kind_hint": "identity", "scope_hint": "user", "confidence_hint": 0.9},
    )
    d = _passthrough_digest_from_obs(obs)
    assert d.worth_remembering is True
    assert d.synthesized_text == "文本"
    assert d.kind == "identity"
    assert d.scope == "user"
    assert d.confidence == 0.9


# ── Phase 3: recall gate ─────────────────────────────────────────


from xmclaw.memory.v2.gateway_recall import (
    classify_buckets_heuristic,
    should_recall_heuristic,
)


def test_gate_short_message():
    assert should_recall_heuristic("ok") is False
    assert should_recall_heuristic("好的") is False


def test_gate_greeting():
    assert should_recall_heuristic("你好") is False
    assert should_recall_heuristic("Hello") is False
    assert should_recall_heuristic("在吗") is False


def test_gate_confirmation():
    assert should_recall_heuristic("没问题") is False
    assert should_recall_heuristic("知道了") is False
    assert should_recall_heuristic("谢谢") is False


def test_gate_substantive_query():
    assert should_recall_heuristic("帮我看看这个代码为什么报错") is True
    assert should_recall_heuristic("我们项目的目标是什么") is True


# ── Phase 3: bucket classification ───────────────────────────────


def test_classify_project():
    buckets = classify_buckets_heuristic("我们网店的账号是 admin")
    assert "project_fact" in buckets


def test_classify_workflow():
    buckets = classify_buckets_heuristic("怎么部署到服务器")
    assert "workflow" in buckets


def test_classify_preference():
    buckets = classify_buckets_heuristic("我喜欢简洁的回复")
    assert "user_preference" in buckets


def test_classify_identity():
    buckets = classify_buckets_heuristic("我是做电商的")
    assert "user_identity" in buckets


def test_classify_rules():
    buckets = classify_buckets_heuristic("永远别删我的配置文件")
    assert "rules" in buckets


def test_classify_multiple_buckets():
    buckets = classify_buckets_heuristic("我们项目的目标怎么设置")
    assert "project_fact" in buckets
    assert "workflow" in buckets


def test_classify_empty():
    buckets = classify_buckets_heuristic("随便聊聊")
    assert buckets == []


# ── Phase 3: targeted_recall ─────────────────────────────────────


class FakeRecallHit:
    def __init__(self, fact, distance=0.1):
        self.fact = fact
        self.distance = distance


@pytest.mark.asyncio
async def test_targeted_recall_filters_by_similarity(fake_svc):
    """Facts below min_similarity are dropped."""
    from xmclaw.memory.v2.models import Fact
    f1 = Fact(id="f1", kind="lesson", scope="project", text="A", bucket="workflow")
    f2 = Fact(id="f2", kind="lesson", scope="project", text="B", bucket="workflow")
    fake_svc.facts = [f1, f2]

    async def mock_recall_hybrid(query, **kwargs):
        return [
            FakeRecallHit(f1, distance=0.1),   # sim = 0.90
            FakeRecallHit(f2, distance=0.5),   # sim = 0.50
        ]

    fake_svc.recall_hybrid = mock_recall_hybrid
    gw = CognitiveMemoryGateway(memory_service=fake_svc)
    hits = await gw.targeted_recall("query", k=4, min_similarity=0.72)
    assert len(hits) == 1
    assert hits[0].text == "A"


@pytest.mark.asyncio
async def test_targeted_recall_excludes_structural_buckets(fake_svc):
    """Static structural-axis buckets are excluded; dynamic ones kept."""
    from xmclaw.memory.v2.models import Fact
    # agent_identity is static (already in system prompt) → excluded.
    f1 = Fact(id="f1", kind="identity", scope="session", text="我是AI助手", bucket="agent_identity")
    # user_identity is dynamic (user may tell us new info mid-session) → kept.
    f2 = Fact(id="f2", kind="identity", scope="user", text="用户叫张三", bucket="user_identity")
    f3 = Fact(id="f3", kind="lesson", scope="project", text="工具用法", bucket="workflow")
    fake_svc.facts = [f1, f2, f3]

    async def mock_recall_hybrid(query, **kwargs):
        return [
            FakeRecallHit(f1, distance=0.1),
            FakeRecallHit(f2, distance=0.1),
            FakeRecallHit(f3, distance=0.1),
        ]

    fake_svc.recall_hybrid = mock_recall_hybrid
    gw = CognitiveMemoryGateway(memory_service=fake_svc)
    hits = await gw.targeted_recall("query", k=4, min_similarity=0.0)
    # agent_identity excluded; user_identity + workflow kept.
    assert len(hits) == 2
    assert hits[0].text == "用户叫张三"
    assert hits[1].text == "工具用法"


# ── Phase 5: metrics ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_metrics_increment_on_ingest(fake_svc):
    """ingest_total and ingest_actions[ADD] bump after successful ingest."""
    gw = CognitiveMemoryGateway(memory_service=fake_svc)
    obs = Observation(source="test", content="hello world", turn_id="t1", timestamp=0.0)
    await gw.ingest(obs)
    m = gw.get_metrics()
    assert m["ingest_total"] == 1
    assert m["ingest_actions"]["ADD"] == 1


@pytest.mark.asyncio
async def test_metrics_dropped_increment(fake_svc):
    """ingest_dropped bumps when THINK says not worth remembering."""
    class _DropLLM:
        async def complete(self, messages, tools=None):
            return type("R", (), {"content": '{"worth_remembering": false, "action": "NOOP", "synthesized_text": "", "reason": "test drop"}'})()

    gw = CognitiveMemoryGateway(
        memory_service=fake_svc, llm=_DropLLM(),
        cfg={"think": {"enabled": True}, "decide": {"enabled": False}},
    )
    obs = Observation(source="test", content="hello world", turn_id="t1", timestamp=0.0)
    await gw.ingest(obs)
    m = gw.get_metrics()
    assert m["ingest_dropped"] == 1


@pytest.mark.asyncio
async def test_metrics_think_cache(fake_svc):
    """think_cache_hits / think_cache_misses track correctly."""
    call_count = 0

    class _CountLLM:
        async def complete(self, messages, tools=None):
            nonlocal call_count
            call_count += 1
            return type("R", (), {"content": '{"worth_remembering": true, "action": "ADD", "synthesized_text": "cached", "reason": "r"}'})()

    gw = CognitiveMemoryGateway(
        memory_service=fake_svc, llm=_CountLLM(),
        cfg={"think": {"enabled": True, "cache_ttl_s": 60}, "decide": {"enabled": False}},
    )
    obs = Observation(source="test", content="same text", turn_id="t1", timestamp=0.0)
    await gw.ingest(obs)
    await gw.ingest(obs)
    m = gw.get_metrics()
    assert m["think_cache_misses"] == 1
    assert m["think_cache_hits"] == 1
    assert call_count == 1


def test_get_metrics_uptime():
    """uptime_s is non-negative and increases over time."""
    import time as _time
    gw = CognitiveMemoryGateway(memory_service=object())
    m1 = gw.get_metrics()
    _time.sleep(0.01)
    m2 = gw.get_metrics()
    assert m2["uptime_s"] >= m1["uptime_s"]


# ── Phase 3: semantic classify ───────────────────────────────────


def test_cosine_similarity_identical():
    """Identical vectors have cosine similarity 1.0."""
    from xmclaw.memory.v2.gateway_recall import _cosine_similarity
    v = [1.0, 2.0, 3.0]
    assert _cosine_similarity(v, v) == 1.0


def test_cosine_similarity_opposite():
    """Opposite vectors have cosine similarity -1.0."""
    from xmclaw.memory.v2.gateway_recall import _cosine_similarity
    assert _cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == -1.0


def test_cosine_similarity_orthogonal():
    """Orthogonal vectors have cosine similarity 0.0."""
    from xmclaw.memory.v2.gateway_recall import _cosine_similarity
    assert _cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0


@pytest.mark.asyncio
async def test_classify_semantic_with_embedder():
    """Semantic classifier returns buckets when embedder is available."""
    from xmclaw.memory.v2.gateway_recall import classify_buckets_semantic

    # Fake embedder: uses a simple hash-based vector so same text
    # yields identical vectors (sim=1.0) and different texts are
    # somewhat orthogonal.
    class _FakeEmbedder:
        async def embed(self, text: str) -> tuple[float, ...]:
            import hashlib
            h = hashlib.sha256(text.encode()).digest()
            # 8-dim vector from hash bytes, normalised.
            vec = [(h[i] - 128) / 128.0 for i in range(8)]
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            return tuple(x / norm for x in vec)

        async def embed_batch(self, texts: list[str]) -> list[tuple[float, ...]]:
            return [await self.embed(t) for t in texts]

    emb = _FakeEmbedder()
    # Since descriptions are different from the query, we won't get
    # high similarity with threshold=0.50.  Use a low threshold to
    # verify the pipeline works end-to-end.
    buckets = await classify_buckets_semantic(
        "帮我部署网站", emb, threshold=0.01, top_k=8,
    )
    assert isinstance(buckets, list)
    # With a random-like embedder we can't assert exact buckets,
    # but we can assert it runs without error and returns <= top_k.
    assert len(buckets) <= 8


@pytest.mark.asyncio
async def test_classify_semantic_no_embedder_fallback():
    """When embedder is None, semantic classifier returns empty list."""
    from xmclaw.memory.v2.gateway_recall import classify_buckets_semantic
    buckets = await classify_buckets_semantic("test", None)
    assert buckets == []


@pytest.mark.asyncio
async def test_recall_uses_semantic_when_embedder_available():
    """recall_for_message_via_gateway prefers semantic classify when
    the memory service has an embedder."""
    from xmclaw.memory.v2.gateway_recall import recall_for_message_via_gateway

    class _FakeSvcWithEmbedder:
        embedder = object()  # any non-None object triggers semantic path

    class _FakeGateway:
        memory_service = _FakeSvcWithEmbedder()

        async def targeted_recall(self, **kwargs):
            # Capture the buckets passed in.
            self.last_buckets = kwargs.get("buckets")
            return []

    gw = _FakeGateway()
    # Patch semantic classifier to return predictable buckets.
    import xmclaw.memory.v2.gateway_recall as _gr
    orig_semantic = _gr.classify_buckets_semantic

    async def _mock_semantic(text, embedder, **kw):
        return ["project_fact"]

    _gr.classify_buckets_semantic = _mock_semantic
    try:
        await recall_for_message_via_gateway(gw, "请问如何部署这个项目到服务器")
        assert gw.last_buckets == ["project_fact"]
    finally:
        _gr.classify_buckets_semantic = orig_semantic


@pytest.mark.asyncio
async def test_recall_falls_back_to_heuristic_when_no_embedder():
    """When memory service has no embedder, recall uses keyword heuristic."""
    from xmclaw.memory.v2.gateway_recall import recall_for_message_via_gateway

    class _FakeSvcNoEmbedder:
        embedder = None

    class _FakeGateway:
        memory_service = _FakeSvcNoEmbedder()

        async def targeted_recall(self, **kwargs):
            self.last_buckets = kwargs.get("buckets")
            return []

    gw = _FakeGateway()
    await recall_for_message_via_gateway(gw, "请问项目部署流程是什么")
    # Keyword heuristic should match "project_fact" + "workflow".
    assert gw.last_buckets is not None
    assert "project_fact" in gw.last_buckets
