"""G5 结构化人格：_compose_persona 合成 system_prompt + _persona_fields 抽取。"""
from __future__ import annotations

from xmclaw.daemon.routers.agents import _compose_persona, _persona_fields


def test_compose_persona_builds_prompt_block() -> None:
    cfg = {"role": "研究员", "goal": "查竞品", "backstory": "十年行研", "style": "严谨"}
    _compose_persona(cfg)
    sp = cfg["system_prompt"]
    assert "【人设】" in sp
    assert "研究员" in sp and "查竞品" in sp and "十年行研" in sp and "严谨" in sp


def test_compose_persona_prepends_to_existing_prompt() -> None:
    cfg = {"role": "写手", "system_prompt": "原有指令：保持简洁"}
    _compose_persona(cfg)
    sp = cfg["system_prompt"]
    assert sp.startswith("【人设】")
    assert "原有指令：保持简洁" in sp


def test_compose_persona_idempotent() -> None:
    cfg = {"role": "写手", "goal": "成稿"}
    _compose_persona(cfg)
    once = cfg["system_prompt"]
    _compose_persona(cfg)  # 再来一次不该叠加
    assert cfg["system_prompt"].count("【人设】") == 1
    assert cfg["system_prompt"] == once


def test_compose_persona_noop_without_fields() -> None:
    cfg = {"system_prompt": "纯指令"}
    _compose_persona(cfg)
    assert cfg["system_prompt"] == "纯指令"


def test_persona_fields_extracts_nonempty() -> None:
    cfg = {"role": "X", "goal": "", "backstory": "B", "other": "ignore"}
    assert _persona_fields(cfg) == {"role": "X", "backstory": "B"}
