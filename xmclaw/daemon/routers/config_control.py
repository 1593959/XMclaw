"""User-facing daemon configuration control API."""
from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Request
from starlette.responses import JSONResponse

from xmclaw.daemon.config_schema import validate_config
from xmclaw.utils.fs_locks import atomic_write_text

router = APIRouter(prefix="/api/v2/config/control", tags=["config-control"])

_SECRET_KEYS = {
    "api_key", "apikey", "bot_token", "app_token", "token",
    "password", "secret", "authorization",
}

CONFIG_FIELDS: dict[str, dict[str, Any]] = {
    "security.prompt_injection": {
        "group": "security", "label": "提示词注入策略",
        "type": "select", "options": ["off", "detect_only", "redact", "block"],
        "restart_required": False,
    },
    "security.guardians.enabled": {
        "group": "security", "label": "启用安全护栏",
        "type": "boolean", "restart_required": False,
    },
    "security.guardians.computer_use_mode": {
        "group": "security", "label": "电脑控制策略",
        "type": "select", "options": ["allow", "approve", "deny"],
        "restart_required": False,
    },
    "tools.enable_bash": {
        "group": "security", "label": "启用 Shell 工具",
        "type": "boolean", "restart_required": True,
    },
    "tools.enable_web": {
        "group": "security", "label": "启用 Web 工具",
        "type": "boolean", "restart_required": True,
    },
    "tools.enable_browser": {
        "group": "security", "label": "启用浏览器工具",
        "type": "boolean", "restart_required": True,
    },
    "tools.shell.execution_policy": {
        "group": "security", "label": "Shell 执行策略",
        "type": "select", "options": ["host_guarded", "docker", "disabled"],
        "restart_required": True,
    },
    "tools.shell.guardrails_mode": {
        "group": "security", "label": "Shell 护栏模式",
        "type": "select", "options": ["strict", "permissive", "disabled"],
        "restart_required": True,
    },
    "tools.shell.sandbox_image": {
        "group": "security", "label": "Shell 沙箱镜像",
        "type": "string", "restart_required": True,
    },
    "tools.allowed_dirs": {
        "group": "security", "label": "允许访问目录",
        "type": "string_list", "restart_required": False,
    },

    "voice.stt.model": {
        "group": "voice", "label": "语音识别模型",
        "type": "string", "restart_required": True,
    },
    "voice.stt.device": {
        "group": "voice", "label": "语音识别设备",
        "type": "select", "options": ["cpu", "cuda", "auto"],
        "restart_required": True,
    },
    "voice.stt.compute_type": {
        "group": "voice", "label": "语音识别计算精度",
        "type": "string", "restart_required": True,
    },
    "voice.stt.language": {
        "group": "voice", "label": "语音识别语言",
        "type": "string", "restart_required": True,
    },
    "voice.tts.voice": {
        "group": "voice", "label": "语音合成声音",
        "type": "string", "restart_required": True,
    },
    "voice.tts.rate": {
        "group": "voice", "label": "语音合成语速",
        "type": "string", "restart_required": True,
    },
    "voice.tts.volume": {
        "group": "voice", "label": "语音合成音量",
        "type": "string", "restart_required": True,
    },

    "evolution.memory.embedding.provider": {
        "group": "models", "label": "向量模型提供方",
        "type": "string", "restart_required": True,
    },
    "evolution.memory.embedding.base_url": {
        "group": "models", "label": "向量模型接口地址",
        "type": "string", "restart_required": True,
    },
    "evolution.memory.embedding.model": {
        "group": "models", "label": "向量模型名称",
        "type": "string", "restart_required": True,
    },
    "evolution.memory.embedding.dimensions": {
        "group": "models", "label": "向量维度",
        "type": "integer", "restart_required": True,
    },
    "evolution.memory.embedding.max_batch_size": {
        "group": "models", "label": "向量批处理大小",
        "type": "integer", "restart_required": True,
    },

    "cognition.memory_v2.enabled": {
        "group": "memory", "label": "启用记忆 v2",
        "type": "boolean", "restart_required": True,
    },
    "cognition.memory_v2.recall_top_k": {
        "group": "memory", "label": "记忆召回数量",
        "type": "integer", "restart_required": False,
    },
    "cognition.memory_v2.gateway.recall.hybrid_enabled": {
        "group": "memory", "label": "启用混合召回",
        "type": "boolean", "restart_required": False,
    },
    "cognition.memory_v2.gateway.recall.gate_enabled": {
        "group": "memory", "label": "启用召回门控",
        "type": "boolean", "restart_required": False,
    },
    "cognition.memory_v2.write_decision.enabled": {
        "group": "memory", "label": "启用写入决策",
        "type": "boolean", "restart_required": False,
    },
    "cognition.memory_v2.candidates.min_quality_score": {
        "group": "memory", "label": "候选记忆最低质量分",
        "type": "number", "restart_required": False,
    },
    "cognition.memory_v2.candidates.auto_reject_below": {
        "group": "memory", "label": "低于该分数自动拒绝",
        "type": "number", "restart_required": False,
    },
    "cognition.memory_v2.candidates.reject_duplicates": {
        "group": "memory", "label": "自动拒绝重复候选",
        "type": "boolean", "restart_required": False,
    },
    "cognition.memory_v2.md_sync.enabled": {
        "group": "memory", "label": "启用 MD 同步",
        "type": "boolean", "restart_required": False,
    },
    "cognition.memory_v2.md_sync.direction": {
        "group": "memory", "label": "MD 同步方向",
        "type": "select",
        "options": ["manual_to_facts", "facts_to_view", "bidirectional_review"],
        "restart_required": False,
    },
    "cognition.memory_v2.md_sync.authority": {
        "group": "memory", "label": "冲突时权威来源",
        "type": "select", "options": ["structured_facts", "manual_md"],
        "restart_required": False,
    },
    "cognition.memory_v2.curator.enabled": {
        "group": "memory", "label": "启用记忆整理器",
        "type": "boolean", "restart_required": False,
    },
    "cognition.self_critique.enabled": {
        "group": "memory", "label": "启用自我反思",
        "type": "boolean", "restart_required": False,
    },

    "skills.disclosure_mode": {
        "group": "skills", "label": "技能展示模式",
        "type": "select", "options": ["auto", "inline", "unified"],
        "restart_required": True,
    },
    "skills.unified_threshold": {
        "group": "skills", "label": "统一技能入口阈值",
        "type": "integer", "restart_required": True,
    },
    "skills.semantic_discovery.enabled": {
        "group": "skills", "label": "启用语义技能发现",
        "type": "boolean", "restart_required": False,
    },
    "skills.semantic_discovery.floor": {
        "group": "skills", "label": "语义发现最低分",
        "type": "number", "restart_required": False,
    },
    "skills.autonomous_invocation.enabled": {
        "group": "skills", "label": "启用技能自主调用",
        "type": "boolean", "restart_required": False,
    },
    "skills.autonomous_invocation.mode": {
        "group": "skills", "label": "自主技能策略",
        "type": "select", "options": ["suggest", "prefer", "force"],
        "restart_required": False,
    },
    "skills.autonomous_invocation.min_score": {
        "group": "skills", "label": "自主技能最低分",
        "type": "number", "restart_required": False,
    },
    "skills.autonomous_invocation.max_loaded": {
        "group": "skills", "label": "每轮最多加载技能数",
        "type": "integer", "restart_required": False,
    },
    "skills.autonomous_invocation.require_decision": {
        "group": "skills", "label": "强制记录技能使用决策",
        "type": "boolean", "restart_required": False,
    },
    "skills.autonomous_invocation.auto_browse_on_no_match": {
        "group": "skills", "label": "无匹配时主动查技能库",
        "type": "boolean", "restart_required": False,
    },
    "skills.install.compatibility_mode": {
        "group": "skills", "label": "安装兼容模式",
        "type": "select", "options": ["strict", "adapt_markdown", "permissive"],
        "restart_required": False,
    },

    "agent.max_hops": {
        "group": "runtime", "label": "最大工具步数",
        "type": "integer", "restart_required": True,
    },
    "agent.max_react_loop": {
        "group": "runtime", "label": "最大 ReAct 循环",
        "type": "integer", "restart_required": True,
    },
    "agent.state_graph.enabled": {
        "group": "runtime", "label": "启用 StateGraph 回合状态",
        "type": "boolean", "restart_required": False,
    },
    "agent.state_graph.emit_phase_events": {
        "group": "runtime", "label": "发送阶段状态事件",
        "type": "boolean", "restart_required": False,
    },
    "agent.failure_strategy.repeat_threshold": {
        "group": "runtime", "label": "连续失败切换阈值",
        "type": "integer", "restart_required": False,
    },
    "agent.failure_strategy.force_strategy_switch": {
        "group": "runtime", "label": "连续失败强制换策略",
        "type": "boolean", "restart_required": False,
    },
    "tools.invoke_timeout_s": {
        "group": "runtime", "label": "工具超时时间（秒）",
        "type": "number", "restart_required": False,
    },
    "cognition.continuous_loop.enabled": {
        "group": "runtime", "label": "连续运行循环",
        "type": "boolean", "restart_required": False,
    },
    "cognition.continuous_loop.autonomy_level": {
        "group": "runtime", "label": "自主等级",
        "type": "integer", "restart_required": False,
    },
    "swarm.enabled": {
        "group": "runtime", "label": "启用子 Agent 群",
        "type": "boolean", "restart_required": True,
    },
    "swarm.max_subagents": {
        "group": "runtime", "label": "最大子 Agent 数",
        "type": "integer", "restart_required": True,
    },
}


@router.get("")
async def get_control_snapshot(request: Request) -> JSONResponse:
    cfg = _current_config(request)
    fields = [
        {
            "path": path,
            **meta,
            "value": _redact_path_value(path, _get_path(cfg, path)),
            "configured": _has_path(cfg, path),
        }
        for path, meta in CONFIG_FIELDS.items()
    ]
    groups: dict[str, list[dict[str, Any]]] = {}
    for field in fields:
        groups.setdefault(field["group"], []).append(field)
    return JSONResponse({
        "ok": True,
        "config_path": _config_path_str(request),
        "groups": groups,
        "fields": fields,
    })


@router.post("/validate")
async def validate_control_patch(
    request: Request,
    body: dict[str, Any] = Body(...),
) -> JSONResponse:
    cfg = _current_config(request)
    result = _apply_patch_to_config(cfg, body.get("patch") or {}, dry_run=True)
    return JSONResponse(_public_result(result), status_code=200 if result["ok"] else 400)


@router.patch("")
async def patch_control_config(
    request: Request,
    body: dict[str, Any] = Body(...),
) -> JSONResponse:
    target = _config_path(request)
    if target is None:
        return JSONResponse(
            {"ok": False, "error": "daemon has no config_path; cannot persist"},
            status_code=500,
        )
    try:
        cfg = _load_disk_config(target)
        result = _apply_patch_to_config(cfg, body.get("patch") or {}, dry_run=False)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    if not result["ok"]:
        return JSONResponse(_public_result(result), status_code=400)
    new_cfg = result["_config"]
    _backup_config(target)
    atomic_write_text(
        target,
        json.dumps(new_cfg, ensure_ascii=False, indent=2) + "\n",
    )
    request.app.state.config = new_cfg
    return JSONResponse({
        **_public_result(result),
        "config_path": str(target),
    })


def _current_config(request: Request) -> dict[str, Any]:
    cfg = getattr(request.app.state, "config", None)
    return copy.deepcopy(cfg) if isinstance(cfg, dict) else {}


def _config_path(request: Request) -> Path | None:
    raw = getattr(request.app.state, "config_path", None)
    return Path(raw) if raw else None


def _config_path_str(request: Request) -> str | None:
    p = _config_path(request)
    return str(p) if p else None


def _load_disk_config(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("config root must be object")
    return data


def _backup_config(path: Path) -> None:
    if not path.exists():
        return
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    atomic_write_text(backup, path.read_text(encoding="utf-8"))


def _apply_patch_to_config(
    cfg: dict[str, Any],
    patch: Any,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    if not isinstance(patch, dict):
        return {"ok": False, "error": "patch must be an object of dotted paths"}
    updated = copy.deepcopy(cfg)
    changed: list[str] = []
    restart_required = False
    try:
        for path, value in patch.items():
            if path not in CONFIG_FIELDS:
                return {"ok": False, "error": f"unsupported config path: {path}"}
            meta = CONFIG_FIELDS[path]
            coerced = _coerce_value(path, value, meta)
            old = _get_path(updated, path)
            if old != coerced:
                _set_path(updated, path, coerced)
                changed.append(path)
                restart_required = restart_required or bool(meta.get("restart_required"))
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    errors = validate_config(updated)
    if errors:
        return {"ok": False, "error": "config validation failed", "errors": errors}
    return {
        "ok": True,
        "dry_run": dry_run,
        "changed": changed,
        "restart_required": restart_required,
        "_config": updated,
    }


def _public_result(result: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in result.items() if k != "_config"}


def _coerce_value(path: str, value: Any, meta: dict[str, Any]) -> Any:
    typ = meta.get("type")
    if value == "" and path.endswith(".language"):
        return None
    if typ == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"{path} must be boolean")
        return value
    if typ == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{path} must be integer")
        return value
    if typ == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{path} must be number")
        return value
    if typ == "string":
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError(f"{path} must be string")
        return value
    if typ == "string_list":
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError(f"{path} must be list of strings")
        return value
    options = meta.get("options")
    if options and value not in options:
        raise ValueError(f"{path} must be one of {', '.join(options)}")
    return value


def _has_path(cfg: dict[str, Any], dotted: str) -> bool:
    cursor: Any = cfg
    for part in dotted.split("."):
        if not isinstance(cursor, dict) or part not in cursor:
            return False
        cursor = cursor[part]
    return True


def _get_path(cfg: dict[str, Any], dotted: str) -> Any:
    cursor: Any = cfg
    for part in dotted.split("."):
        if not isinstance(cursor, dict):
            return None
        cursor = cursor.get(part)
    return cursor


def _set_path(cfg: dict[str, Any], dotted: str, value: Any) -> None:
    cursor: dict[str, Any] = cfg
    parts = dotted.split(".")
    for part in parts[:-1]:
        nxt = cursor.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cursor[part] = nxt
        cursor = nxt
    cursor[parts[-1]] = value


def _redact_path_value(path: str, value: Any) -> Any:
    key = path.split(".")[-1].lower()
    if key in _SECRET_KEYS:
        if isinstance(value, str) and value:
            return f"<redacted ...{value[-4:]}>"
        return "<unset>"
    return value


__all__ = ["CONFIG_FIELDS", "router"]
