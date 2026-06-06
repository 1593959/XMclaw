"""Agents API — CRUD for the running :class:`MultiAgentManager` registry.

Epic #17 Phase 3. Mounted at ``/api/v2/agents``. Unlike Epic #18's
``/api/v2/workspaces`` router (which edits abstract user presets),
this one drives live daemon state: each POST instantiates a fresh
``Workspace`` + AgentLoop and registers it in
``app.state.agents``. The primary agent — the one built from the
top-level daemon config and hanging off ``app.state.agent`` — is
NOT exposed through this surface; it has id ``"main"`` reserved and
is routed to by WS clients that omit the ``agent_id`` query param.

Why a distinct surface from ``/workspaces``? Presets are
"configurations the user might launch"; agents are "configurations
currently running". The lifecycle, sensitivity, and wipe semantics
differ (see docstrings on :func:`agents_registry_dir` /
:func:`workspaces_dir`). Two routers keep those concerns from
contaminating each other.
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

from xmclaw.daemon.multi_agent_manager import (
    AgentIdError,
    MultiAgentManager,
    _sanitize_id,
)

router = APIRouter(prefix="/api/v2/agents", tags=["agents"])


_RESERVED_IDS = frozenset({"main"})

# G5: 结构化人格字段（CrewAI 式）。存在 agent config 顶层，既喂给编排器选讲/
# 派活（见 routers/rooms.py:_persona_of），也合成进 system_prompt 让 agent 真按
# 人格说话。
_PERSONA_KEYS = ("role", "goal", "backstory", "style")


def _compose_persona(config: dict[str, Any]) -> None:
    """把 role/goal/backstory/style 合成成一段中文人设，前置进 system_prompt（原位改）。

    幂等：用一对标记夹住人设块，重复 create 不会叠加。无任何人格字段则不动。
    """
    parts: list[str] = []
    role = str(config.get("role") or "").strip()
    goal = str(config.get("goal") or "").strip()
    backstory = str(config.get("backstory") or "").strip()
    style = str(config.get("style") or "").strip()
    if role:
        parts.append(f"你的身份是「{role}」。")
    if goal:
        parts.append(f"你的核心目标：{goal}")
    if backstory:
        parts.append(f"背景设定：{backstory}")
    if style:
        parts.append(f"表达风格：{style}")
    if not parts:
        return
    block = "【人设】\n" + "\n".join(parts)
    existing = str(config.get("system_prompt") or "").strip()
    # 去掉旧人设块（若有），避免叠加
    import re as _re
    existing = _re.sub(r"【人设】[\s\S]*?(?=\n\n|\Z)", "", existing).strip()
    config["system_prompt"] = (block + ("\n\n" + existing if existing else "")).strip()


def _persona_fields(cfg: dict[str, Any]) -> dict[str, Any]:
    """从 config 抽出人格字段供 UI 展示（仅非空）。"""
    if not isinstance(cfg, dict):
        return {}
    return {k: cfg[k] for k in _PERSONA_KEYS if cfg.get(k)}


def _manager(request: Request) -> MultiAgentManager | None:
    """Pull the manager off ``app.state``.

    Returns None in echo-mode apps (``create_app()`` with no config)
    where no manager was wired in. Routes branch on that so tests
    that don't care about multi-agent don't need a manager fixture.
    """
    return getattr(request.app.state, "agents", None)


def _workspace_summary(
    agent_id: str, ws: Any, *, is_primary: bool,
) -> dict[str, Any]:
    """B-131: enrich the agent row with what UIs need to make sense of it.

    Returns kind / model / tool_count / system_prompt preview so the
    UI doesn't show 8 rows of indistinguishable agent_ids.
    """
    base: dict[str, Any] = {
        "agent_id": agent_id,
        "ready": ws.is_ready() if ws is not None else True,
        "primary": is_primary,
        "kind": "llm",
    }
    if ws is None:
        return base
    kind = getattr(ws, "kind", "llm")
    base["kind"] = kind
    cfg = getattr(ws, "config", None) or {}
    if isinstance(cfg, dict):
        _llm_raw = cfg.get("llm")
        llm_cfg: dict[str, Any] = _llm_raw if isinstance(_llm_raw, dict) else {}
        # Pick the most informative model name we can find. config.llm
        # has both "provider" + "model" — show "provider/model" so a
        # row showing two skills with the same model name still
        # distinguishes them by provider.
        provider = llm_cfg.get("provider") or ""
        model = llm_cfg.get("model") or ""
        if provider and model:
            base["model"] = f"{provider}/{model}"
        elif model:
            base["model"] = model
        # System prompt preview (first 120 chars) — lets the UI show
        # WHAT this agent is supposed to do, not just its id.
        sp = cfg.get("system_prompt") or llm_cfg.get("system_prompt") or ""
        if isinstance(sp, str) and sp.strip():
            base["system_prompt_preview"] = sp.strip()[:120]
        base.update(_persona_fields(cfg))
    loop = getattr(ws, "agent_loop", None)
    if loop is not None:
        tools = getattr(loop, "_tools", None)
        if tools is not None and hasattr(tools, "list_tools"):
            try:
                base["tool_count"] = len(tools.list_tools())
            except Exception:  # noqa: BLE001 — UI hint only
                pass
    return base


def _primary_summary(request: Request) -> dict[str, Any]:
    """B-131: synthesise the same enriched row for the primary agent.

    The primary lives on ``app.state.agent`` instead of the manager —
    pull config / model from there so 'main' shows up in the UI with
    parity to user-launched agents.
    """
    base: dict[str, Any] = {
        "agent_id": "main",
        "ready": True,
        "primary": True,
        "kind": "llm",
    }
    primary = getattr(request.app.state, "agent", None)
    if primary is None:
        return base
    cfg = getattr(request.app.state, "config", None) or {}
    if isinstance(cfg, dict):
        _llm_raw = cfg.get("llm")
        llm_cfg: dict[str, Any] = _llm_raw if isinstance(_llm_raw, dict) else {}
        provider = llm_cfg.get("provider") or ""
        model = llm_cfg.get("model") or ""
        if provider and model:
            base["model"] = f"{provider}/{model}"
        elif model:
            base["model"] = model
        sp = cfg.get("system_prompt") or llm_cfg.get("system_prompt") or ""
        if isinstance(sp, str) and sp.strip():
            base["system_prompt_preview"] = sp.strip()[:120]
        base.update(_persona_fields(cfg))
    tools = getattr(primary, "_tools", None)
    if tools is not None and hasattr(tools, "list_tools"):
        try:
            base["tool_count"] = len(tools.list_tools())
        except Exception:  # noqa: BLE001
            pass
    return base


@router.get("")
async def list_agents(request: Request) -> JSONResponse:
    """Return every registered agent, with the primary flagged.

    The primary — the config-built ``app.state.agent`` — is emitted
    synthetically so UIs can show it alongside user-launched agents
    without a special-case branch on the client side.
    """
    manager = _manager(request)
    items: list[dict[str, Any]] = []

    # Synthetic entry for the primary agent, if one exists.
    primary = getattr(request.app.state, "agent", None)
    if primary is not None:
        items.append(_primary_summary(request))

    if manager is not None:
        for agent_id in manager.list_ids():
            if agent_id in _RESERVED_IDS:
                # Shouldn't happen (create rejects reserved ids), but
                # if it somehow did, don't double-list.
                continue
            ws = manager.get(agent_id)
            items.append(_workspace_summary(agent_id, ws, is_primary=False))

    return JSONResponse({"agents": items})


@router.post("")
async def create_agent(request: Request) -> JSONResponse:
    """Launch a new agent. Body: ``{"agent_id": "...", "config": {...}}``.

    Upsert-via-DELETE semantics are intentional — POST to an existing
    id returns 409 rather than silently replacing, because replacing
    would drop the AgentLoop's in-memory session history without
    warning. The caller must DELETE first if they really want to
    rebuild.
    """
    manager = _manager(request)
    if manager is None:
        return JSONResponse(
            {"ok": False, "error": "multi-agent registry not configured"},
            status_code=503,
        )

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)

    raw_id = body.get("agent_id") or body.get("id") or ""
    if not isinstance(raw_id, str) or not raw_id.strip():
        return JSONResponse(
            {"ok": False, "error": "agent_id required"}, status_code=400
        )
    agent_id = raw_id.strip()
    if agent_id in _RESERVED_IDS:
        return JSONResponse(
            {"ok": False, "error": f"agent_id {agent_id!r} is reserved"},
            status_code=400,
        )
    if agent_id != _sanitize_id(agent_id):
        return JSONResponse(
            {
                "ok": False,
                "error": "agent_id may only contain [A-Za-z0-9_-]",
            },
            status_code=400,
        )
    if agent_id in manager:
        return JSONResponse(
            {"ok": False, "error": "already exists"}, status_code=409
        )

    config = body.get("config") or {}
    if not isinstance(config, dict):
        return JSONResponse(
            {"ok": False, "error": "config must be an object"}, status_code=400
        )

    # G5: 顶层也接受 role/goal/backstory/style（UI 直接传），并入 config。
    for k in _PERSONA_KEYS:
        if k in body and k not in config:
            config[k] = body[k]
    # 把结构化人格合成进 system_prompt（agent 真按人设说话）。
    _compose_persona(config)

    try:
        ws = await manager.create(agent_id, config)
    except AgentIdError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001 — surface as HTTP 500 instead of stalling
        return JSONResponse(
            {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
            status_code=500,
        )

    return JSONResponse(
        {"ok": True, "agent_id": agent_id, "ready": ws.is_ready()},
        status_code=201,
    )


@router.delete("/{agent_id}")
async def delete_agent(agent_id: str, request: Request) -> JSONResponse:
    """Tear down a running agent.

    404 on a missing id is deliberate — idempotent DELETE that hides
    "already gone" races the tabs. The primary ``main`` agent is not
    deletable through this surface; stop the whole daemon instead.
    """
    manager = _manager(request)
    if manager is None:
        return JSONResponse(
            {"ok": False, "error": "multi-agent registry not configured"},
            status_code=503,
        )
    if agent_id in _RESERVED_IDS:
        return JSONResponse(
            {"ok": False, "error": f"cannot delete reserved agent {agent_id!r}"},
            status_code=400,
        )
    removed = await manager.remove(agent_id)
    if not removed:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    return JSONResponse({"ok": True})


@router.get("/{agent_id}")
async def get_agent(agent_id: str, request: Request) -> JSONResponse:
    """Inspect one agent's readiness + primary flag."""
    if agent_id == "main":
        primary = getattr(request.app.state, "agent", None)
        if primary is None:
            return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
        return JSONResponse({"agent_id": "main", "ready": True, "primary": True})
    manager = _manager(request)
    if manager is None:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    ws = manager.get(agent_id)
    if ws is None:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    return JSONResponse(_workspace_summary(agent_id, ws, is_primary=False))
