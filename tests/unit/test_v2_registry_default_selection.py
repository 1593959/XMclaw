"""回归：build_llm_registry_from_config 的默认 profile 选择（2026-06-13）。

事故：5f747b8 (B-146) 删了 ``if default_id is None: default_id = prof.id``
兜底却没接 docstring 承诺的替代逻辑，``default_id=None`` 被写死。后果——
config 有命名 profile 但无 legacy ``llm.anthropic`` 块、也无显式
``default_profile_id`` 时，registry 有 profile 却没默认 → AgentLoop 无模型
可选 → 对话死、模型选择器空。

锁 docstring 的 4 步选择顺序。
"""
from __future__ import annotations

from xmclaw.daemon.factory import build_llm_registry_from_config


def _profile(pid: str, model: str = "m") -> dict:
    return {
        "id": pid,
        "provider": "openai_compat",
        "model": model,
        "api_key": "sk-x",
        "base_url": "https://example.com/v1",
    }


def test_first_profile_default_when_no_explicit_no_legacy() -> None:
    """事故核心场景：命名 profile + 无 legacy 块 + 无 default_profile_id
    → 默认回退到第一个 profile（而非 None）。"""
    cfg = {"llm": {"profiles": [_profile("alpha"), _profile("beta")]}}
    reg = build_llm_registry_from_config(cfg)
    assert set(reg.profiles) == {"alpha", "beta"}
    assert reg.default_id == "alpha", "无显式默认时应回退首个 profile，不能是 None"
    assert reg.default() is not None


def test_explicit_default_profile_id_wins() -> None:
    cfg = {
        "llm": {
            "default_profile_id": "beta",
            "profiles": [_profile("alpha"), _profile("beta")],
        }
    }
    reg = build_llm_registry_from_config(cfg)
    assert reg.default_id == "beta"


def test_explicit_default_ignored_when_not_loaded() -> None:
    """指向不存在/被禁用的 profile → 回退首个，不是 None。"""
    cfg = {
        "llm": {
            "default_profile_id": "ghost",
            "profiles": [_profile("alpha")],
        }
    }
    reg = build_llm_registry_from_config(cfg)
    assert reg.default_id == "alpha"


def test_disabled_profile_not_chosen_as_default() -> None:
    """enabled:false 的 profile 不进 registry，故不会被选为默认。"""
    cfg = {
        "llm": {
            "profiles": [
                {**_profile("off"), "enabled": False},
                _profile("on"),
            ]
        }
    }
    reg = build_llm_registry_from_config(cfg)
    assert "off" not in reg.profiles
    assert reg.default_id == "on"


def test_empty_profiles_default_none() -> None:
    """真正没有 profile → default_id=None（echo 模式），不报错。"""
    reg = build_llm_registry_from_config({"llm": {"profiles": []}})
    assert reg.default_id is None
