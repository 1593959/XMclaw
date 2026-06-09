"""Embedding 召回三修(2026-06-08):

1. 本地端点直连(绕系统代理)—— root cause of "召回全是关键词"。
2. Qwen3 query instruct 非对称前缀。
3. 查询重排相关性主导(RANK_W_RELEVANCE 提权)。
"""
from __future__ import annotations

import pytest

from xmclaw.providers.memory.embedding import OpenAIEmbeddingProvider as EmbeddingProvider


# ── ① 本地端点检测(决定 httpx trust_env / urllib 代理)──
@pytest.mark.parametrize("url,expect", [
    ("http://localhost:11434/v1", True),
    ("http://127.0.0.1:11434/v1", True),
    ("http://[::1]:8000/v1", True),
    ("http://0.0.0.0:1234/v1", True),
    ("https://api.openai.com/v1", False),
    ("https://dashscope.aliyuncs.com/compatible-mode/v1", False),
])
def test_is_local_detection(url, expect):
    p = EmbeddingProvider(api_key="x", base_url=url, model="m", dimensions=8)
    assert p._is_local is expect


def test_local_endpoint_available_without_key():
    # 本地端点免 key 可用(Ollama)
    p = EmbeddingProvider(base_url="http://localhost:11434/v1", model="m", dimensions=8)
    assert p.is_available() is True
    # 远程无 key 不可用
    p2 = EmbeddingProvider(base_url="https://api.openai.com/v1", model="m", dimensions=8)
    assert p2.is_available() is False


# ── ② Qwen3 query instruct 前缀(非对称嵌入)──
class _FakeProvider:
    name = "openai"
    dim = 4

    def __init__(self, model):
        self.model = model

    async def embed(self, texts):
        return [[0.0] * self.dim for _ in texts]

    def is_available(self):
        return True


def test_query_instruct_added_for_qwen():
    from xmclaw.memory.v2.embedding import EmbeddingService
    svc = EmbeddingService(_FakeProvider("qwen3-embedding:0.6b"), cache_capacity=0)
    out = svc._query_instruct("我的偏好是什么")
    assert out.startswith("Instruct:") and "Query: 我的偏好是什么" in out


def test_query_instruct_skipped_for_symmetric_model():
    from xmclaw.memory.v2.embedding import EmbeddingService
    svc = EmbeddingService(_FakeProvider("text-embedding-3-small"), cache_capacity=0)
    assert svc._query_instruct("我的偏好是什么") == "我的偏好是什么"


def test_query_instruct_no_model_attr_is_noop():
    from xmclaw.memory.v2.embedding import EmbeddingService, StubEmbedder
    svc = EmbeddingService(StubEmbedder(dim=4), cache_capacity=0)
    assert svc._query_instruct("hi") == "hi"  # stub 无 model → 不加前缀


@pytest.mark.asyncio
async def test_embed_query_differs_from_embed_for_qwen():
    """query 与同文本 document 嵌入应不同(前缀不同)→ 缓存键也不同。"""
    from xmclaw.memory.v2.embedding import EmbeddingService

    seen = []

    class _Capture(_FakeProvider):
        async def embed(self, texts):
            seen.extend(texts)
            return [[0.1] * self.dim for _ in texts]

    svc = EmbeddingService(_Capture("qwen3-embedding"), cache_capacity=0)
    await svc.embed("同样的文本")          # document
    await svc.embed_query("同样的文本")     # query
    assert seen[0] == "同样的文本"
    assert seen[1].startswith("Instruct:")  # query 带前缀


# ── ③ 查询重排:相关性主导 ──
def test_relevance_weight_dominates():
    from xmclaw.memory.v2 import service
    assert service.RANK_W_RELEVANCE >= 3.0 * service.RANK_W_RECENCY
    assert service.RANK_W_RELEVANCE >= 3.0 * service.RANK_W_IMPORTANCE
