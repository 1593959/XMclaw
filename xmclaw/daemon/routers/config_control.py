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


def _field(
    group: str,
    label: str,
    typ: str,
    *,
    description: str,
    restart_required: bool = False,
    options: list[str] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "group": group,
        "label": label,
        "description": description,
        "type": typ,
        "restart_required": restart_required,
    }
    if options is not None:
        out["options"] = options
    return out


CONFIG_FIELDS: dict[str, dict[str, Any]] = {
    "security.prompt_injection": _field(
        "security", "提示词注入策略", "select",
        description="控制网页、文件、工具输出中的提示词注入风险如何处理。",
        options=["off", "detect_only", "redact", "block"],
    ),
    "security.guardians.enabled": _field(
        "security", "启用安全护栏", "boolean",
        description="打开后会对高风险工具和输入做额外检查。",
    ),
    "security.guardians.computer_use_mode": _field(
        "security", "电脑控制策略", "select",
        description="控制桌面自动化是否直接允许、需要确认或拒绝。",
        options=["allow", "approve", "deny"],
    ),
    "tools.enable_bash": _field(
        "security", "启用 Shell 工具", "boolean",
        description="允许 Agent 调用本地命令行工具。",
        restart_required=True,
    ),
    "tools.enable_web": _field(
        "security", "启用 Web 工具", "boolean",
        description="允许 Agent 使用联网搜索和网页读取能力。",
        restart_required=True,
    ),
    "tools.enable_browser": _field(
        "security", "启用浏览器工具", "boolean",
        description="允许 Agent 控制真实浏览器或浏览器自动化工具。",
        restart_required=True,
    ),
    "tools.shell.execution_policy": _field(
        "security", "Shell 执行策略", "select",
        description="选择命令在宿主机护栏下运行、Docker 沙箱运行或完全禁用。",
        options=["host_guarded", "docker", "disabled"],
        restart_required=True,
    ),
    "tools.shell.guardrails_mode": _field(
        "security", "Shell 护栏模式", "select",
        description="严格模式会阻止更多危险命令，宽松模式只拦截明显高危操作。",
        options=["strict", "permissive", "disabled"],
        restart_required=True,
    ),
    "tools.shell.sandbox_image": _field(
        "security", "Shell 沙箱镜像", "string",
        description="Docker 沙箱模式使用的镜像名称。",
        restart_required=True,
    ),
    "tools.allowed_dirs": _field(
        "security", "允许访问目录", "string_list",
        description="文件和命令工具优先遵守的本地目录 allowlist。",
    ),

    "voice.stt.model": _field(
        "voice", "语音识别模型", "string",
        description="STT 使用的模型名称，例如 Whisper/faster-whisper 模型。",
        restart_required=True,
    ),
    "voice.stt.device": _field(
        "voice", "语音识别设备", "select",
        description="STT 推理设备，cpu/cuda/auto。",
        options=["cpu", "cuda", "auto"],
        restart_required=True,
    ),
    "voice.stt.compute_type": _field(
        "voice", "语音识别计算精度", "string",
        description="faster-whisper 等后端使用的 compute_type。",
        restart_required=True,
    ),
    "voice.stt.language": _field(
        "voice", "语音识别语言", "string",
        description="留空表示自动检测，常用值如 zh/en。",
        restart_required=True,
    ),
    "voice.tts.voice": _field(
        "voice", "语音合成声音", "string",
        description="TTS 声音名称，例如 zh-CN-XiaoxiaoNeural。",
        restart_required=True,
    ),
    "voice.tts.rate": _field(
        "voice", "语音合成语速", "string",
        description="TTS 语速，例如 +0%、+10%、-10%。",
        restart_required=True,
    ),
    "voice.tts.volume": _field(
        "voice", "语音合成音量", "string",
        description="TTS 音量，例如 +0%、+20%、-20%。",
        restart_required=True,
    ),

    "evolution.memory.embedding.provider": _field(
        "models", "向量模型提供方", "string",
        description="记忆和技能语义检索使用的 embedding provider。",
        restart_required=True,
    ),
    "evolution.memory.embedding.base_url": _field(
        "models", "向量模型接口地址", "string",
        description="OpenAI-compatible embedding API base URL。",
        restart_required=True,
    ),
    "evolution.memory.embedding.model": _field(
        "models", "向量模型名称", "string",
        description="用于生成记忆向量的模型名。",
        restart_required=True,
    ),
    "evolution.memory.embedding.dimensions": _field(
        "models", "向量维度", "integer",
        description="必须与当前向量库表结构一致，修改后通常需要重建索引。",
        restart_required=True,
    ),
    "evolution.memory.embedding.max_batch_size": _field(
        "models", "向量批处理大小", "integer",
        description="embedding 批量请求的最大条数。",
        restart_required=True,
    ),

    "cognition.memory_v2.enabled": _field(
        "memory", "启用记忆 v2", "boolean",
        description="启用结构化长期记忆、候选审核和召回管线。",
        restart_required=True,
    ),
    "cognition.memory_v2.recall_top_k": _field(
        "memory", "记忆召回数量", "integer",
        description="每轮最多注入多少条相关记忆。",
    ),
    "cognition.memory_v2.gateway.recall.hybrid_enabled": _field(
        "memory", "启用混合召回", "boolean",
        description="同时使用向量、BM25 和结构化 bucket 进行召回。",
    ),
    "cognition.memory_v2.gateway.recall.gate_enabled": _field(
        "memory", "启用召回门控", "boolean",
        description="跳过寒暄和无记忆价值的短消息，降低噪声。",
    ),
    "cognition.memory_v2.gateway.recall.classify_enabled": _field(
        "memory", "启用召回分类", "boolean",
        description="先判断任务需要哪些记忆类型，再定向检索。",
    ),
    "cognition.memory_v2.write_decision.enabled": _field(
        "memory", "启用写入决策", "boolean",
        description="写入前执行 ADD/UPDATE/DELETE/NOOP 与候选审核策略。",
    ),
    "cognition.memory_v2.candidates.min_quality_score": _field(
        "memory", "候选记忆最低质量分", "number",
        description="低于该分数的候选需要人工审核或被拒绝。",
    ),
    "cognition.memory_v2.candidates.auto_reject_below": _field(
        "memory", "自动拒绝分数线", "number",
        description="低于该分数的自动候选会直接被拒绝。",
    ),
    "cognition.memory_v2.candidates.reject_duplicates": _field(
        "memory", "自动拒绝重复候选", "boolean",
        description="检测重复候选，避免记忆库越用越乱。",
    ),
    "cognition.memory_v2.md_sync.enabled": _field(
        "memory", "启用 MD 同步", "boolean",
        description="把用户手动维护的 MD 内容导入结构化记忆；不把自动事实默认写回 MD。",
    ),
    "cognition.memory_v2.md_sync.direction": _field(
        "memory", "MD 同步方向", "select",
        description="控制 MD 与结构化事实之间的同步方式。",
        options=["manual_to_facts", "facts_to_view", "bidirectional_review"],
    ),
    "cognition.memory_v2.md_sync.authority": _field(
        "memory", "冲突时权威来源", "select",
        description="MD 与结构化事实冲突时优先相信哪一侧。",
        options=["structured_facts", "manual_md"],
    ),
    "cognition.memory_v2.curator.enabled": _field(
        "memory", "启用记忆整理器", "boolean",
        description="定期归并、降权、去重和整理长期记忆。",
    ),
    "cognition.self_critique.enabled": _field(
        "memory", "启用自我反思", "boolean",
        description="失败后生成反思候选，但不直接把未验证经验固化。",
    ),

    "skills.disclosure_mode": _field(
        "skills", "技能展示模式", "select",
        description="控制技能作为独立工具、统一入口或自动模式暴露给模型。",
        options=["auto", "inline", "unified"],
        restart_required=True,
    ),
    "skills.unified_threshold": _field(
        "skills", "统一技能入口阈值", "integer",
        description="超过该数量后优先使用统一技能入口降低工具噪声。",
        restart_required=True,
    ),
    "skills.semantic_discovery.enabled": _field(
        "skills", "启用语义技能发现", "boolean",
        description="用向量语义匹配技能，解决中文任务看不到英文技能的问题。",
    ),
    "skills.semantic_discovery.floor": _field(
        "skills", "语义发现最低分", "number",
        description="低于该分数的技能不会被主动推荐。",
    ),
    "skills.autonomous_invocation.enabled": _field(
        "skills", "启用技能自主调用", "boolean",
        description="要求 Agent 主动查找、选择、使用或解释跳过技能。",
    ),
    "skills.autonomous_invocation.mode": _field(
        "skills", "自主技能策略", "select",
        description="suggest 只提示，prefer 优先使用，force 强制先查技能。",
        options=["suggest", "prefer", "force"],
    ),
    "skills.autonomous_invocation.min_score": _field(
        "skills", "自主技能最低分", "number",
        description="低于该分数的技能不会进入候选。",
    ),
    "skills.autonomous_invocation.max_loaded": _field(
        "skills", "每轮最多加载技能数", "integer",
        description="限制每轮注入模型的技能数量。",
    ),
    "skills.autonomous_invocation.require_decision": _field(
        "skills", "强制记录技能使用决策", "boolean",
        description="要求模型说明使用、跳过或查询技能库的理由。",
    ),
    "skills.autonomous_invocation.auto_browse_on_no_match": _field(
        "skills", "无匹配时主动查技能库", "boolean",
        description="没有直接候选时，先调用 skill_browse 再决定是否使用通用工具。",
    ),
    "skills.install.compatibility_mode": _field(
        "skills", "安装兼容模式", "select",
        description="处理 Claude Code/agentskills 等外部格式时的转换策略。",
        options=["strict", "adapt_markdown", "permissive"],
    ),

    "agent.max_hops": _field(
        "runtime", "最大工具步数", "integer",
        description="单轮 Agent 最多执行多少个工具 hop。",
        restart_required=True,
    ),
    "agent.max_react_loop": _field(
        "runtime", "最大 ReAct 循环", "integer",
        description="限制显式 ReAct 循环次数，防止无限思考/行动。",
        restart_required=True,
    ),
    "agent.state_graph.enabled": _field(
        "runtime", "启用 StateGraph 回合状态", "boolean",
        description="用 reducer-backed GraphState 记录并驱动每轮主要阶段。",
    ),
    "agent.state_graph.enforce_phase_order": _field(
        "runtime", "强制 StateGraph 阶段顺序", "boolean",
        description="开启后 recall、skill、prompt、hop、writeback 必须按图状态推进。",
    ),
    "agent.state_graph.emit_phase_events": _field(
        "runtime", "发送阶段状态事件", "boolean",
        description="向前端和日志广播 recall/skill/prompt/hop/writeback 阶段状态。",
    ),
    "agent.failure_strategy.repeat_threshold": _field(
        "runtime", "连续失败切换阈值", "integer",
        description="同类失败达到该次数后强制换策略。",
    ),
    "agent.failure_strategy.force_strategy_switch": _field(
        "runtime", "连续失败强制换策略", "boolean",
        description="启用后同类失败会要求先查记忆、技能、产物并更换方法。",
    ),
    "tools.invoke_timeout_s": _field(
        "runtime", "工具超时时间（秒）", "number",
        description="单个工具调用的默认超时时间。",
    ),
    "automation.runtime.observe_required": _field(
        "automation", "强制先观察", "boolean",
        description="自动化动作执行前必须先生成 BrowserObservation 或 ScreenObservation。",
    ),
    "automation.runtime.verify_after_action": _field(
        "automation", "动作后强验证", "boolean",
        description="点击、输入、滚动、导航后默认采集 before/after 并返回变化证据。",
    ),
    "automation.runtime.trace_enabled": _field(
        "automation", "启用 Trace / Replay", "boolean",
        description="把 observe、action、verification 和 recovery 写入 JSONL trace，便于复盘。",
    ),
    "automation.browser.dom_ref_diff": _field(
        "automation", "浏览器 DOM/ref diff", "boolean",
        description="浏览器动作后比较 DOM、refs、activeElement 和输入值变化。",
    ),
    "automation.browser.input_value_verify": _field(
        "automation", "浏览器输入值校验", "boolean",
        description="fill/type 后读取真实 input value，防止只调用成功但页面未生效。",
    ),
    "automation.computer.hard_route_switch": _field(
        "automation", "桌面硬换路", "boolean",
        description="连续无视觉变化时阻断同一路线，要求切换 UIA、OCR、SOM 或询问用户。",
    ),
    "automation.computer.no_change_threshold": _field(
        "automation", "无变化阈值", "integer",
        description="桌面自动化连续多少次无视觉变化后必须换路。",
    ),
    "automation.computer.capture_after_default": _field(
        "automation", "动作后默认截图", "boolean",
        description="桌面点击、输入、滚动后默认截图并计算视觉变化。",
    ),
    "cognition.continuous_loop.enabled": _field(
        "runtime", "连续运行循环", "boolean",
        description="启用后台认知循环和主动任务。",
    ),
    "cognition.continuous_loop.autonomy_level": _field(
        "runtime", "自主等级", "integer",
        description="后台主动性等级，越高越积极。",
    ),
    "swarm.enabled": _field(
        "runtime", "启用子 Agent 群", "boolean",
        description="允许复杂任务拆分给多个子 Agent。",
        restart_required=True,
    ),
    "swarm.max_subagents": _field(
        "runtime", "最大子 Agent 数", "integer",
        description="单轮任务最多并发多少个子 Agent。",
        restart_required=True,
    ),
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
    if options:
        if value == "" or value is None:
            # 对于未设置过的 select 字段，使用第一个选项作为默认值
            # 这样老配置文件缺少新字段时不会报错
            return options[0]
        if value not in options:
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
