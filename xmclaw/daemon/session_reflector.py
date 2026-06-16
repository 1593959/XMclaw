"""SessionReflector — periodic cross-session memory consolidation.

User ask (2026-06-16): 每隔 N 分钟扫描所有会话，把值得记住的东西总结进
长期记忆——**做了什么、踩了什么坑（报错/工具失败/被拦截）、怎么改的、
经验教训**。配套的对话内"每阶段主动总结"走 turn-end 反思（见
``agent_loop`` 的 ``_reflect_turn_lessons``）+ persona prompt 引导；本模块
是后台的"批处理"半边。

设计照搬 :class:`xmclaw.daemon.dream_compactor.DreamCron` 的 ticker 模式：
后台任务按间隔调 :meth:`SessionReflector.reflect_once`。成本控制：

* **增量** — 每会话持久化一个 ts 水位（``reflector_state.json``），每次
  只重读自上次以来有新事件的会话。
* 用**主 LLM**（用户在 2026-06-16 选了质量优先）。
* 每 tick 限制会话数 + 每会话转写长度上限，避免单次烧爆。

抽出的事实经 :meth:`MemoryService.remember`（kind=LESSON/EPISODE/…）落库，
因此已有的去重 / contradicts 流水线照常生效。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from xmclaw.utils.log import get_logger

if TYPE_CHECKING:
    from xmclaw.providers.llm.base import LLMProvider

_log = get_logger(__name__)


# 参与反思的事件类型——能体现"做了什么 / 坑 / 修复 / 决策"的信号。
_SALIENT_TYPES = (
    "user_message",
    "llm_response",
    "tool_invocation_finished",
    "plan_failed",
    "plan_step_failed",
    "plan_completed",
    "turn_blocked",
    "agent_asked_question",
    "user_answered_question",
)

# 一次反思单会话最多吃多少事件 / 多少字符（防超长 prompt）。
_MAX_EVENTS_PER_SESSION = 120
_MAX_TRANSCRIPT_CHARS = 12_000
# 自上次水位以来新事件少于这个数就跳过——不值得为一两条消息跑 LLM。
_MIN_NEW_EVENTS = 4


REFLECT_PROMPT_ZH = """\
你是 XMclaw 的"会话复盘器"——一个后台子流程，负责把一段会话里**值得长期
记住的经验**提炼成原子事实，写进 agent 的记忆。

下面是一段会话转写（按时间顺序）。请只提炼**对未来真正有用**的条目：

- **踩的坑 / 报错**：遇到了什么问题、错误信息、为什么会发生
- **怎么修的**：采用了什么解决方案 / 改动
- **经验教训**：下次该怎么做、什么做法行不通、什么约束要记住
- **关键决策**：做了什么技术/方向选择，及其理由

**不要**提炼：寒暄、与未来无关的一次性闲聊、用户的个人琐事（那些有别的
管道）、无结论的中间步骤。

每条事实：一句话、自包含、客观。给每条标一个 kind：
  lesson（教训/经验）| correction（错误及其修正）| decision（决策）
  | episode（值得记住的事件经过）

输出**严格的 JSON 数组**，无其他文字、无 markdown 围栏：
[{"text": "...", "kind": "lesson", "confidence": 0.0-1.0}, ...]
没有值得记的就输出 []。
"""


def _default_state_path() -> Path:
    # 与 events.db 同目录（~/.xmclaw/v2/），方便一起备份/清理。
    from xmclaw.core.bus.sqlite import default_events_db_path
    return default_events_db_path().parent / "reflector_state.json"


class SessionReflector:
    """Reads recent session events and distils long-term lessons into memory."""

    def __init__(
        self,
        *,
        llm: "LLMProvider",
        memory_service: Any,
        events_db_path: Path | str | None = None,
        state_path: Path | str | None = None,
        bus: Any = None,
        max_sessions_per_tick: int = 20,
        lookback_days: float = 14.0,
    ) -> None:
        self._llm = llm
        self._memory = memory_service
        if events_db_path is None:
            from xmclaw.core.bus.sqlite import default_events_db_path
            events_db_path = default_events_db_path()
        self._events_db = Path(events_db_path)
        self._state_path = Path(state_path) if state_path else _default_state_path()
        self._bus = bus
        self._max_sessions = max(1, int(max_sessions_per_tick))
        self._lookback_s = max(3600.0, float(lookback_days) * 86400.0)

    # ── state (per-session ts watermark) ──────────────────────────────

    def _load_state(self) -> dict[str, float]:
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
            wm = raw.get("watermarks") if isinstance(raw, dict) else None
            return {str(k): float(v) for k, v in (wm or {}).items()}
        except Exception:  # noqa: BLE001 — missing/corrupt → start fresh
            return {}

    def _save_state(self, watermarks: dict[str, float]) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(
                json.dumps({"watermarks": watermarks, "updated_at": time.time()},
                           ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("session_reflector.state_save_failed err=%s", exc)

    # ── event reads (direct sqlite — read-only) ───────────────────────

    def _changed_sessions(self, watermarks: dict[str, float], now: float) -> list[str]:
        """Session ids with events newer than their watermark, oldest-first."""
        from xmclaw.daemon.session_store import is_internal_session_id
        try:
            con = sqlite3.connect(f"file:{self._events_db}?mode=ro", uri=True)
        except Exception as exc:  # noqa: BLE001
            _log.warning("session_reflector.db_open_failed err=%s", exc)
            return []
        try:
            rows = con.execute(
                "SELECT session_id, MAX(ts) FROM events WHERE ts >= ? "
                "GROUP BY session_id",
                (now - self._lookback_s,),
            ).fetchall()
        except Exception as exc:  # noqa: BLE001
            _log.warning("session_reflector.scan_failed err=%s", exc)
            return []
        finally:
            con.close()
        changed: list[tuple[str, float]] = []
        for sid, last in rows:
            sid = str(sid or "")
            if not sid or is_internal_session_id(sid):
                continue
            if float(last or 0) > watermarks.get(sid, 0.0) + 0.0:
                changed.append((sid, watermarks.get(sid, 0.0)))
        # oldest watermark first → fairness, the most-stale get caught up first
        changed.sort(key=lambda t: t[1])
        return [sid for sid, _ in changed[: self._max_sessions]]

    def _events_since(self, sid: str, since_ts: float) -> list[tuple[float, str, dict]]:
        try:
            con = sqlite3.connect(f"file:{self._events_db}?mode=ro", uri=True)
        except Exception:  # noqa: BLE001
            return []
        try:
            placeholders = ",".join("?" * len(_SALIENT_TYPES))
            rows = con.execute(
                f"SELECT ts, type, payload FROM events WHERE session_id = ? "
                f"AND ts > ? AND type IN ({placeholders}) ORDER BY ts ASC "
                f"LIMIT {_MAX_EVENTS_PER_SESSION}",
                (sid, since_ts, *_SALIENT_TYPES),
            ).fetchall()
        except Exception:  # noqa: BLE001
            return []
        finally:
            con.close()
        out: list[tuple[float, str, dict]] = []
        for ts, typ, payload in rows:
            try:
                pl = json.loads(payload) if payload else {}
            except Exception:  # noqa: BLE001
                pl = {}
            out.append((float(ts or 0), str(typ or ""), pl if isinstance(pl, dict) else {}))
        return out

    @staticmethod
    def _render_transcript(events: list[tuple[float, str, dict]]) -> str:
        lines: list[str] = []
        for _ts, typ, pl in events:
            if typ == "user_message":
                txt = str(pl.get("content") or "").strip()
                if txt:
                    lines.append(f"[用户] {txt[:600]}")
            elif typ == "llm_response":
                txt = str(pl.get("content") or pl.get("text") or "").strip()
                ok = pl.get("ok") is not False
                if not ok:
                    lines.append(f"[助手·失败] {str(pl.get('error') or '')[:300]}")
                elif txt:
                    lines.append(f"[助手] {txt[:600]}")
            elif typ == "tool_invocation_finished":
                name = str(pl.get("name") or pl.get("tool_name") or "tool")
                ok = pl.get("ok") is not False and not pl.get("error")
                if not ok:
                    err = str(pl.get("error") or pl.get("result") or "")[:300]
                    lines.append(f"[工具失败·{name}] {err}")
            elif typ in ("plan_failed", "plan_step_failed"):
                lines.append(f"[计划失败] {str(pl.get('error') or pl.get('reason') or '')[:300]}")
            elif typ == "turn_blocked":
                lines.append(f"[回合被拦截] {str(pl.get('message') or pl.get('reason') or '')[:300]}")
            elif typ == "agent_asked_question":
                lines.append(f"[助手提问] {str(pl.get('question') or '')[:300]}")
            elif typ == "user_answered_question":
                lines.append(f"[用户回答] {str(pl.get('answer') or pl.get('content') or '')[:300]}")
        text = "\n".join(lines)
        if len(text) > _MAX_TRANSCRIPT_CHARS:
            text = text[-_MAX_TRANSCRIPT_CHARS:]
        return text

    # ── one reflection pass ───────────────────────────────────────────

    async def reflect_once(self) -> dict[str, Any]:
        """Scan changed sessions, distil lessons, persist to memory."""
        if self._memory is None or self._llm is None:
            return {"ok": False, "error": "llm or memory_service not wired"}
        now = time.time()
        watermarks = self._load_state()
        sessions = self._changed_sessions(watermarks, now)
        if not sessions:
            return {"ok": True, "sessions": 0, "facts": 0}

        total_facts = 0
        processed = 0
        for sid in sessions:
            since = watermarks.get(sid, 0.0)
            events = self._events_since(sid, since)
            if len(events) < _MIN_NEW_EVENTS:
                # not enough new signal — bump watermark so we don't re-scan
                if events:
                    watermarks[sid] = max(e[0] for e in events)
                continue
            transcript = self._render_transcript(events)
            if not transcript.strip():
                watermarks[sid] = max(e[0] for e in events)
                continue
            try:
                facts = await self._distil(sid, transcript)
            except Exception as exc:  # noqa: BLE001
                _log.warning("session_reflector.distil_failed sid=%s err=%s", sid[:16], exc)
                continue
            for f in facts:
                try:
                    await self._remember(sid, f)
                    total_facts += 1
                except Exception as exc:  # noqa: BLE001
                    _log.warning("session_reflector.remember_failed err=%s", exc)
            watermarks[sid] = max(e[0] for e in events)
            processed += 1

        self._save_state(watermarks)
        _log.info("session_reflector.done sessions=%d facts=%d", processed, total_facts)
        return {"ok": True, "sessions": processed, "facts": total_facts}

    async def _distil(self, sid: str, transcript: str) -> list[dict[str, Any]]:
        from xmclaw.providers.llm.base import Message
        msgs = [
            Message(role="system", content=REFLECT_PROMPT_ZH),
            Message(role="user", content=f"会话转写：\n\n{transcript}"),
        ]
        resp = await self._llm.complete(msgs)
        raw = (getattr(resp, "content", "") or "").strip()
        # tolerate accidental ```json fences
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.lower().startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        try:
            data = json.loads(raw)
        except Exception:  # noqa: BLE001
            return []
        if not isinstance(data, list):
            return []
        out: list[dict[str, Any]] = []
        for it in data:
            if not isinstance(it, dict):
                continue
            text = str(it.get("text") or "").strip()
            if not text:
                continue
            out.append({
                "text": text,
                "kind": str(it.get("kind") or "lesson").lower(),
                "confidence": float(it.get("confidence") or 0.7),
            })
        return out

    async def _remember(self, sid: str, fact: dict[str, Any]) -> None:
        from xmclaw.memory.v2.models import FactKind, FactScope
        _KIND_MAP = {
            "lesson": FactKind.LESSON,
            "correction": FactKind.CORRECTION,
            "decision": FactKind.DECISION,
            "episode": FactKind.EPISODE,
        }
        kind = _KIND_MAP.get(fact["kind"], FactKind.LESSON)
        # failure_modes 是注册的 lesson-kind bucket（坑/教训渲染目的地）；
        # corrections/decisions 也归到这儿，统一进 MEMORY.md 的经验区。
        await self._memory.remember(
            fact["text"],
            kind=kind,
            scope=FactScope.PROJECT,
            confidence=max(0.0, min(1.0, fact["confidence"])),
            provenance="session_reflector",
            bucket="failure_modes",
        )


class ReflectorCron:
    """Background task that fires :meth:`SessionReflector.reflect_once` every
    ``interval_minutes`` (mirrors :class:`DreamCron`)."""

    def __init__(self, *, reflector: SessionReflector, interval_minutes: float = 30.0) -> None:
        self._reflector = reflector
        self._interval_s = max(60.0, float(interval_minutes) * 60.0)
        self._task: asyncio.Task | None = None
        self._stopped = asyncio.Event()
        self._last_run_at: float | None = None
        self._last_result: dict[str, Any] | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def last_run_at(self) -> float | None:
        return self._last_run_at

    @property
    def last_result(self) -> dict[str, Any] | None:
        return self._last_result

    async def start(self) -> None:
        if self.is_running:
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._loop(), name="reflector-cron")

    async def stop(self) -> None:
        self._stopped.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._task = None

    async def _loop(self) -> None:
        while not self._stopped.is_set():
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self._interval_s)
                return  # stopped
            except asyncio.TimeoutError:
                pass
            try:
                self._last_result = await self._reflector.reflect_once()
                self._last_run_at = time.time()
            except Exception as exc:  # noqa: BLE001
                _log.warning("reflector_cron.failed err=%s", exc)
                self._last_result = {"ok": False, "error": str(exc)}
                self._last_run_at = time.time()
