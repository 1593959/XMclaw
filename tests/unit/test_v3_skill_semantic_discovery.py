"""§⑫ autonomous-invocation fix — semantic skill discovery.

Pins the language-agnostic fix: a CJK query with ZERO token overlap
against English skill descriptions still surfaces the right skill via
embedding cosine, so the agent can autonomously call it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from xmclaw.skills.prefilter import select_relevant_skills
from xmclaw.skills.semantic_index import SkillSemanticIndex


# ── minimal ToolSpec-like stub (prefilter only reads name/description/
#    parameters_schema) ────────────────────────────────────────────


@dataclass
class _Spec:
    name: str
    description: str = ""
    parameters_schema: dict = field(default_factory=dict)


# ── a controllable fake embedder: maps text → vector by keyword, so we
#    can make "提交代码" (CJK) and "git commit" (EN) land close ───────


class _KeywordEmbedder:
    """Deterministic, semantically-meaningful-enough stub: each text is
    embedded as a bag-of-concepts vector over a fixed concept list, where
    both the Chinese and English surface forms of a concept map to the
    same axis. This lets us test cross-language cosine without a real
    model."""

    dim = 3
    # concept axis -> (chinese markers, english markers)
    _CONCEPTS = [
        (("提交", "代码", "git"), ("commit", "git", "code")),
        (("天气",), ("weather", "forecast")),
        (("翻译",), ("translate", "translation")),
    ]

    def _vec(self, text: str) -> list[float]:
        t = text.lower()
        v = [0.0] * self.dim
        for i, (zh, en) in enumerate(self._CONCEPTS):
            if any(m in text for m in zh) or any(m in t for m in en):
                v[i] = 1.0
        return v

    async def embed(self, text: str) -> tuple[float, ...]:
        return tuple(self._vec(text))

    async def embed_batch(self, texts: list[str]) -> list[tuple[float, ...]]:
        return [tuple(self._vec(t)) for t in texts]


# ── semantic index ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_semantic_index_cross_language_match():
    idx = SkillSemanticIndex(_KeywordEmbedder())
    specs = [
        _Spec("skill_git-commit", "Create a git commit with a message"),
        _Spec("skill_weather", "Get the weather forecast"),
    ]
    # Chinese query, English descriptions — token overlap would be ZERO.
    scores = await idx.scores("帮我提交代码", specs, floor=0.3)
    assert scores.get("skill_git-commit", 0.0) > 0.3
    # The unrelated weather skill is below the floor → omitted.
    assert "skill_weather" not in scores


@pytest.mark.asyncio
async def test_semantic_index_no_query_returns_empty():
    idx = SkillSemanticIndex(_KeywordEmbedder())
    assert await idx.scores("", [_Spec("skill_x", "desc")]) == {}


@pytest.mark.asyncio
async def test_semantic_index_embed_failure_is_silent():
    class _BoomEmbedder:
        dim = 3
        async def embed(self, text):  # noqa: ANN001
            raise RuntimeError("provider down")
        async def embed_batch(self, texts):  # noqa: ANN001
            raise RuntimeError("provider down")

    idx = SkillSemanticIndex(_BoomEmbedder())
    # Must degrade to {} — never raise into the turn.
    assert await idx.scores("提交代码", [_Spec("skill_git", "git commit")]) == {}


@pytest.mark.asyncio
async def test_semantic_index_caches_descriptions():
    emb = _KeywordEmbedder()
    calls = {"batch": 0}
    _orig = emb.embed_batch

    async def _counting_batch(texts):
        calls["batch"] += 1
        return await _orig(texts)

    emb.embed_batch = _counting_batch  # type: ignore[method-assign]
    idx = SkillSemanticIndex(emb)
    specs = [_Spec("skill_git-commit", "Create a git commit")]
    await idx.scores("提交", specs)
    await idx.scores("代码", specs)  # same specs → no re-embed
    assert calls["batch"] == 1


# ── prefilter fusion: the actual autonomous-invocation fix ─────────


def _many_decoys(n: int) -> list[_Spec]:
    return [_Spec(f"skill_decoy{i}", f"does unrelated thing number {i}") for i in range(n)]


def test_prefilter_cjk_query_surfaces_skill_via_semantic():
    """The core regression: a Chinese query with zero token overlap
    against an English skill description must still surface that skill
    when a semantic score is supplied."""
    target = _Spec("skill_git-commit", "Create a git commit with a message")
    specs = _many_decoys(40) + [target]
    # Without semantic scores: pure token overlap → target dropped
    # (CJK query shares no tokens with the English description).
    out_token_only = select_relevant_skills("帮我提交代码", specs, top_k=12)
    names_token = {s.name for s in out_token_only}
    assert "skill_git-commit" not in names_token  # the bug, reproduced

    # With a semantic score for the target: it clears the > 0 gate and
    # surfaces — the agent can now see + autonomously call it.
    out_semantic = select_relevant_skills(
        "帮我提交代码", specs, top_k=12,
        semantic_scores={"skill_git-commit": 0.82},
    )
    names_sem = {s.name for s in out_semantic}
    assert "skill_git-commit" in names_sem


def test_prefilter_semantic_does_not_revive_path_gated_skill():
    """An explicit path opt-out (s = -1.0) must still veto even with a
    semantic score — author intent wins over fuzzy match."""
    gated = _Spec(
        "skill_only-for-tsx",
        "format tsx files",
        parameters_schema={"x_paths": ["**/*.tsx"]},
    )
    specs = _many_decoys(40) + [gated]
    out = select_relevant_skills(
        "提交代码", specs, top_k=12,
        active_paths=["src/main.py"],  # .py, not .tsx → gate fires
        semantic_scores={"skill_only-for-tsx": 0.9},
    )
    assert "skill_only-for-tsx" not in {s.name for s in out}


def test_prefilter_no_semantic_scores_is_unchanged():
    """semantic_scores=None must behave exactly like before (no regression)."""
    specs = _many_decoys(40) + [_Spec("skill_git-commit", "git commit helper")]
    a = select_relevant_skills("git commit", specs, top_k=12)
    b = select_relevant_skills("git commit", specs, top_k=12, semantic_scores=None)
    assert [s.name for s in a] == [s.name for s in b]
