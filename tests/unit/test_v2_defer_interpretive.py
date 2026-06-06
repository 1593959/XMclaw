"""regex 写入降级：defer_interpretive=True 时主观/解释性类交给 LLM，不再强写。

- 客观类（URL/账号…）始终强写（确定性兜底）。
- 主观类（qual_goal/preference/correction/org）仅在 defer=True 时跳过。
"""
from __future__ import annotations

import itertools

import pytest

from xmclaw.memory.v2.key_info_extractor import (
    _INTERPRETIVE_PATTERNS,
    extract_and_remember,
    extract_keys,
)


class _FakeFact:
    _ids = itertools.count()

    def __init__(self, text: str) -> None:
        self.id = f"f{next(self._ids)}"
        self.text = text


class _FakeMem:
    def __init__(self) -> None:
        self.remembered: list[str] = []

    async def remember(self, text, **kw):
        self.remembered.append(text)
        return _FakeFact(text)

    async def relate(self, *a, **k):
        return None


@pytest.mark.asyncio
async def test_defer_skips_interpretive_keeps_objective() -> None:
    # 含主观(qual_goal "希望提升满意度") + 客观(URL)
    msg = "希望提升客户满意度 https://example.com"
    mem = _FakeMem()
    await extract_and_remember(msg, mem, defer_interpretive=True)
    joined = " ".join(mem.remembered)
    assert "example.com" in joined          # 客观类仍写
    assert "提升客户满意度" not in joined    # 主观目标交给 LLM，不强写


@pytest.mark.asyncio
async def test_no_defer_writes_everything() -> None:
    msg = "希望提升客户满意度 https://example.com"
    mem = _FakeMem()
    await extract_and_remember(msg, mem, defer_interpretive=False)
    joined = " ".join(mem.remembered)
    assert "example.com" in joined
    assert "提升客户满意度" in joined        # 无 LLM 时确定性兜底全写


def test_interpretive_set_matches_real_pattern_names() -> None:
    # 防御：集合里的名字必须是 extract_keys 真用到的 pattern_name
    names = {k.pattern_name for k in extract_keys(
        "希望提升满意度。我喜欢简短。不要再啰嗦。我们公司叫蜂之巢")}
    # 至少命中一个解释性 pattern，且集合是其超集语义（不强求全覆盖此样本）
    assert _INTERPRETIVE_PATTERNS & names
