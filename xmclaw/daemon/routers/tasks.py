"""Tasks API — Mission Control 的任务聚合视图（Phase 10.M1.3）。

Mounted at ``/api/v2/tasks``. 只读聚合：任务不是新的存储实体，而是对
既有事实（SessionStore 会话 × 事件流里的 plan_*/todo_updated/
agent_asked_question/llm_response）的投影。增量更新走既有 WS 事件，
本端点只服务启动水化（设计规格 docs/MISSION_CONTROL_DESIGN_2026.md §2.2）。

状态推导（启发式，按优先级）：
    awaiting_input  有未回答的 ask_user_question（asked > answered）
    running         最后事件是非终态执行事件且足够新（< STALE_S）
    failed          最后的 plan_failed / llm_response ok=false
    done            出现过 plan_completed，或最后 llm_response 正常收尾
    chat            没有任何 plan/todo 信号的纯对话

不动 AgentLoop；bus 不可查询（无持久化后端）时退化为纯 session 列表。
"""
from __future__ import annotations

import re
import time
from typing import Any

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

from xmclaw.daemon.session_store import SessionStore, is_internal_session_id
from xmclaw.utils.paths import default_sessions_db_path

router = APIRouter(prefix="/api/v2/tasks", tags=["tasks"])

# 最后执行事件多旧之后不再算 running（daemon 重启/断流的兜底）。
STALE_S = 180.0

# 参与状态推导的事件类型（一次 IN 查询全取，按时间升序）。
_SALIENT_TYPES = [
    "user_message",  # 任务标题源（首条真实用户指令）
    "plan_started",
    "plan_step_started",
    "plan_step_completed",
    "plan_step_failed",
    "plan_completed",
    "plan_failed",
    "todo_updated",
    "agent_asked_question",
    "user_answered_question",
    "llm_request",
    "llm_response",
    "tool_call_emitted",
    "tool_invocation_finished",
    "task_state_changed",
]

_RUNNING_TAIL = {
    "llm_request",
    "tool_call_emitted",
    "tool_invocation_finished",
    "plan_step_started",
    "plan_started",
}


# user 消息尾部搭载的注入块（session-workspace 提示 / memory 注入等，
# 见 agent_loop.py）。preview 里剥掉，否则任务标题全是 "<memory-v2-facts>"。
# 标签名单与 webui/src/lib/reducer.ts 的 INJECTED_BLOCKS 保持同步。
_INJECTED_BLOCKS = re.compile(
    r"<(session-workspace|output_schema|memory-[\w-]+|recalled-memory-files"
    r"|recalled|curriculum-[\w-]+)>[\s\S]*?</\1>"
)


def _clean_title(preview: str, sid: str) -> str:
    text = _INJECTED_BLOCKS.sub("", preview or "")
    # 残缺注入块（preview 截断导致闭合标签丢失）：砍掉首个 '<tag' 起的尾巴。
    text = re.sub(r"<[a-z][\w-]*>?[\s\S]*$", "", text) if text.lstrip().startswith("<") else text
    text = " ".join(text.split())
    return text[:60] or sid


def _store() -> SessionStore | None:
    try:
        return SessionStore(default_sessions_db_path())
    except Exception:  # noqa: BLE001
        return None


def _derive(events: list[Any], now: float) -> dict[str, Any]:
    """从一个 session 的事件序列推导任务快照字段。"""
    asked = 0
    answered = 0
    plan_total = 0
    plan_done = 0
    todo_total = 0
    todo_done = 0
    saw_plan_or_todo = False
    plan_terminal: str | None = None
    last_type = ""
    last_ts = 0.0
    last_resp_ok: bool | None = None
    last_resp_more_hops = False
    first_user_text = ""

    for ev in events:
        raw_t = getattr(ev, "type", "")
        t = str(getattr(raw_t, "value", raw_t) or "")  # EventType enum 或裸 str 都接受
        payload = getattr(ev, "payload", None) or {}
        ts = float(getattr(ev, "ts", 0.0) or 0.0)
        last_type, last_ts = t, ts
        if t == "user_message":
            if not first_user_text:
                first_user_text = str(payload.get("content") or "")
        elif t == "agent_asked_question":
            asked += 1
        elif t == "user_answered_question":
            answered += 1
        elif t == "plan_started":
            saw_plan_or_todo = True
            plan_total = int(payload.get("n_steps") or len(payload.get("step_ids") or []))
            plan_done = 0
            plan_terminal = None
        elif t == "plan_step_completed":
            plan_done += 1
        elif t in ("plan_completed", "plan_failed"):
            plan_terminal = str(payload.get("status") or ("failed" if t == "plan_failed" else "completed"))
        elif t == "todo_updated":
            saw_plan_or_todo = True
            items = payload.get("items") or []
            if isinstance(items, list):
                todo_total = len(items)
                todo_done = sum(
                    1 for it in items
                    if isinstance(it, dict) and it.get("status") == "completed"
                )
        elif t == "llm_response":
            last_resp_ok = payload.get("ok") is not False
            last_resp_more_hops = bool(payload.get("tool_calls_count") or 0)

    steps_total = plan_total or todo_total
    steps_done = plan_done if plan_total else todo_done

    # awaiting_input 仅当未答问题真的挂在事件流尾部（最后事件就是提问）。
    # 旧条件只看 asked>answered 计数 —— 历史会话里被弃置的提问会让任务
    # 永远顶着"等你回答"，用户实测点名误导（2026-06-12）。
    if asked > answered and last_type == "agent_asked_question":
        status = "awaiting_input"
    elif last_type in _RUNNING_TAIL and (now - last_ts) < STALE_S:
        status = "running"
    elif last_type == "llm_response" and last_resp_more_hops and (now - last_ts) < STALE_S:
        status = "running"
    elif plan_terminal == "failed" or last_resp_ok is False:
        status = "failed"
    elif plan_terminal in ("completed", "repaired"):
        status = "done"
    elif saw_plan_or_todo and steps_total > 0 and steps_done >= steps_total:
        status = "done"
    elif not saw_plan_or_todo:
        status = "chat"
    else:
        status = "done" if last_resp_ok else "chat"

    return {
        "status": status,
        "steps_total": steps_total,
        "steps_done": steps_done,
        "last_activity": last_type,
        "updated_at": last_ts,
        "_first_user_text": first_user_text,
    }


@router.get("")
async def list_tasks(request: Request, limit: int = 30) -> JSONResponse:
    """任务快照列表，最近活动倒序。bus 不可查询时退化为 chat 态列表。"""
    store = _store()
    if store is None:
        return JSONResponse({"tasks": [], "error": "session_store unavailable"})
    requested = max(1, min(int(limit), 100))
    try:
        rows = store.list_recent(limit=min(200, requested * 4))
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"tasks": [], "error": str(exc)})
    rows = [
        r for r in rows
        if not is_internal_session_id(str(r.get("session_id") or ""))
    ][:requested]

    bus = getattr(request.app.state, "bus", None)
    can_query = bus is not None and hasattr(bus, "query")
    now = time.time()
    # Authoritative "is this turn actually live" signal — a session whose
    # turn died mid-flight keeps a recent event tail and would otherwise
    # show "running" for STALE_S. Only sessions with a live, not-done turn
    # task are truly running.
    _live = getattr(request.app.state, "active_turn_tasks", {}) or {}

    def _is_live(sid: str) -> bool:
        t = _live.get(sid)
        return t is not None and not getattr(t, "done", lambda: True)()

    def _truthful_status(sid: str, status: str) -> str:
        # Derived "running" but no live turn → it stopped; show idle so the
        # user can resume instead of staring at a frozen "运行中".
        if status == "running" and not _is_live(sid):
            return "chat"
        return status

    tasks: list[dict[str, Any]] = []
    for r in rows:
        sid = str(r.get("session_id") or "")
        preview = str(r.get("preview") or "")
        snap: dict[str, Any] = {
            "sid": sid,
            "title": _clean_title(preview, sid),
            "status": "chat",
            "steps_total": 0,
            "steps_done": 0,
            "updated_at": float(r.get("updated_at") or 0.0),
            "last_activity": "",
        }
        if can_query:
            try:
                events = bus.query(session_id=sid, types=_SALIENT_TYPES, limit=500)
                if events:
                    derived = _derive(events, now)
                    # session 行的 updated_at 比事件流晚（持久化在后），取较大者。
                    derived["updated_at"] = max(derived["updated_at"], snap["updated_at"])
                    # 事件流里的首条用户指令是更可靠的标题源（store preview
                    # 常被注入块污染/截断）。
                    user_text = derived.pop("_first_user_text", "")
                    if user_text:
                        cleaned = _clean_title(user_text, sid)
                        if cleaned != sid:
                            snap["title"] = cleaned
                    snap.update(derived)
            except Exception:  # noqa: BLE001
                pass  # 单 session 推导失败不拖垮整个列表
        snap["status"] = _truthful_status(sid, snap["status"])
        tasks.append(snap)

    # 2026-06-16: surface sessions that are LIVE in the event log but not
    # yet in session_store (persisted only at turn-END). Without this, a
    # task the user is mid-way through — or one they refreshed away from
    # before it finished — never appears in the rail and looks lost, even
    # though it's fully recoverable from events.db.
    if can_query:
        try:
            covered = {t["sid"] for t in tasks}
            # Direct GROUP BY on the durable log: reliably finds EVERY
            # session active in the last 24h, regardless of total event
            # volume (a recent-N scan misses older-but-unpersisted sessions
            # once a chatty run floods the tail).
            import sqlite3 as _sql
            from xmclaw.core.bus.sqlite import default_events_db_path
            _con = _sql.connect(str(default_events_db_path()))
            try:
                _rows = _con.execute(
                    "SELECT session_id, MAX(ts) FROM events WHERE ts >= ? "
                    "GROUP BY session_id ORDER BY MAX(ts) DESC LIMIT 300",
                    (now - 86400.0,),
                ).fetchall()
            finally:
                _con.close()
            extra_ts: dict[str, float] = {}
            for sid, last in _rows:
                sid = str(sid or "")
                if not sid or sid in covered or is_internal_session_id(sid):
                    continue
                extra_ts[sid] = float(last or 0)
            for sid in sorted(extra_ts, key=lambda s: extra_ts[s], reverse=True)[:requested]:
                events = bus.query(session_id=sid, types=_SALIENT_TYPES, limit=500)
                if not events:
                    continue
                derived = _derive(events, now)
                snap = {
                    "sid": sid, "title": sid, "status": "chat",
                    "steps_total": 0, "steps_done": 0,
                    "updated_at": 0.0, "last_activity": "",
                }
                ut = derived.pop("_first_user_text", "")
                if ut:
                    c = _clean_title(ut, sid)
                    if c != sid:
                        snap["title"] = c
                snap.update(derived)
                snap["status"] = _truthful_status(sid, snap["status"])
                tasks.append(snap)
        except Exception:  # noqa: BLE001
            pass

    tasks.sort(key=lambda t: t["updated_at"], reverse=True)
    return JSONResponse({"tasks": tasks})
