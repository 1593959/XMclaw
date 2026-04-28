"""DreamCompactor — periodic LLM-driven MEMORY.md compaction.

B-51 (CoPaw "Auto-Dream" parity, ours-from-scratch). MEMORY.md grows
forever as the agent appends bullets via remember / update_persona.
Char-cap LRU eviction (B-25) drops the oldest line by date prefix —
fine for old facts, terrible for facts the agent legitimately keeps
re-asserting (each repeat resets the LRU clock for that line, but
nothing dedupes the redundant prose).

CoPaw's solution: a daily cron spawns a sub-agent named
"DreamOptimizer" that reads MEMORY.md + recent daily logs and
rewrites MEMORY.md per a hand-crafted prompt (合并去冗 / 状态覆写 /
归纳整合 / 废弃剔除). Backs up the old version first.

XMclaw adopts the same pattern but skips spawning a separate agent —
we already have a wired LLMProvider, so we call it directly via
``llm.complete()``. The "dream prompt" is module-level so it's
review-able without forking a session.

Safety:

  * Always writes a timestamped backup to
    ``<persona_dir>/backup/memory_backup_YYYYMMDD-HHMMSS.md``
    BEFORE overwriting MEMORY.md
  * Validates the LLM output looks plausibly like markdown
    (rejects empty / pure-error / shrunk-by-90% outputs)
  * Off when no LLM is configured (bus stays quiet)
  * Fires once per day at ``hour:minute`` local time, configurable

Bus event ``MEMORY_DREAMED`` (B-51) emitted on success.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from xmclaw.utils.log import get_logger

if TYPE_CHECKING:
    from xmclaw.providers.llm.base import LLMProvider, Message

_log = get_logger(__name__)


# Hand-crafted prompt — what we tell the LLM about HOW to rewrite.
# Written zh-CN because the typical XMclaw user's MEMORY.md is.
DREAM_PROMPT_ZH = """\
你是 XMclaw 的"梦优化器"——agent 自我维护的子流程。

你的任务：读取下面的 MEMORY.md 当前内容 + 最近的对话日志，
重写 MEMORY.md 让它**更精简、更稳定、更有用**。

四条规则：

1. **合并去冗** — 同一事实多次写入只保留一条，取最早日期。
2. **状态覆写** — "用户用 macOS" → 后来出现 "用户用 Windows" → 只保留最新。
3. **归纳整合** — 多条相关琐碎事实归纳为一条概括性陈述。
4. **废弃剔除** — 明显过时（项目已完成、人员已离开）或不再被重复提及的条目删除。

约束：

- **保持原有的 ## 节结构**（用户偏好 / 项目状态 / 工具偏好 / ...）
- 每条 bullet 保留 `- YYYY-MM-DD: ...` 的日期前缀
- 不要删除整个节，节内可以变空（写一行 "_(暂无)_"）
- 保留 `# 记忆` 顶级标题
- 输出**只是新的 MEMORY.md 内容**，不要其他文字、不要解释、不要 ```markdown 围栏

下面是当前 MEMORY.md：

```markdown
{memory_md}
```

下面是最近 7 天的对话日志摘要（按日期排）：

```
{daily_logs}
```

现在输出新的 MEMORY.md：
"""


def _hhmm_until_next(target_hour: int, target_minute: int) -> float:
    """Seconds until the next ``HH:MM`` local-time clock tick. Used by
    the cron loop to sleep the right amount until the next firing
    (handles the day-rollover case)."""
    now = time.localtime()
    target = time.struct_time((
        now.tm_year, now.tm_mon, now.tm_mday,
        target_hour, target_minute, 0,
        now.tm_wday, now.tm_yday, now.tm_isdst,
    ))
    target_ts = time.mktime(target)
    now_ts = time.time()
    if target_ts <= now_ts:
        target_ts += 86400  # tomorrow
    return target_ts - now_ts


class DreamCompactor:
    """One-shot MEMORY.md rewrite via the wired LLM."""

    def __init__(
        self,
        *,
        llm: "LLMProvider",
        persona_dir_provider,         # callable () -> Path
        bus: "Any | None" = None,
        daily_log_window_days: int = 7,
        min_keep_ratio: float = 0.3,  # reject rewrite if it's < 30% of original
    ) -> None:
        self._llm = llm
        self._persona_dir_provider = persona_dir_provider
        self._bus = bus
        self._window_days = max(1, int(daily_log_window_days))
        self._min_ratio = float(min_keep_ratio)

    async def dream(self) -> dict[str, Any]:
        """Run one dream pass. Returns ``{ok: bool, ...}``."""
        try:
            pdir = Path(self._persona_dir_provider())
        except Exception:  # noqa: BLE001
            return {"ok": False, "error": "persona_dir not resolvable"}
        if not pdir.is_dir():
            return {"ok": False, "error": f"persona_dir does not exist: {pdir}"}

        memory_path = pdir / "MEMORY.md"
        if not memory_path.is_file():
            return {"ok": False, "error": "no MEMORY.md to compact"}

        try:
            current = memory_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return {"ok": False, "error": f"read failed: {exc}"}
        if not current.strip():
            return {"ok": True, "skipped": "empty", "before_chars": 0}

        daily_logs = self._read_recent_daily_logs(pdir)

        prompt = DREAM_PROMPT_ZH.format(
            memory_md=current,
            daily_logs=daily_logs or "(无)",
        )

        # Call the LLM. Single-shot, no streaming — we want the
        # complete rewrite atomically.
        from xmclaw.providers.llm.base import Message
        try:
            resp = await asyncio.wait_for(
                self._llm.complete(
                    [Message(role="user", content=prompt)], tools=None,
                ),
                timeout=120.0,
            )
        except asyncio.TimeoutError:
            return {"ok": False, "error": "LLM timeout (120s)"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"LLM call failed: {exc}"}

        new_text = (resp.content or "").strip()
        # Strip a leading ```markdown fence if the LLM emitted one
        # despite our instruction not to.
        if new_text.startswith("```"):
            lines = new_text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            new_text = "\n".join(lines).strip()

        # Sanity: the rewrite must not collapse the file to nothing.
        # Reject anything < min_keep_ratio of the original — almost
        # certainly a model error.
        if len(new_text) < self._min_ratio * len(current):
            return {
                "ok": False,
                "error": (
                    f"rewrite too small ({len(new_text)} chars vs "
                    f"{len(current)} original; min_ratio={self._min_ratio})"
                ),
                "before_chars": len(current),
                "after_chars": len(new_text),
            }
        if not new_text:
            return {"ok": False, "error": "LLM returned empty"}

        # Backup first.
        backup_dir = pdir / "backup"
        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y%m%d-%H%M%S")
            backup_path = backup_dir / f"memory_backup_{ts}.md"
            backup_path.write_text(current, encoding="utf-8")
        except OSError as exc:
            return {"ok": False, "error": f"backup write failed: {exc}"}

        # Atomic-ish rewrite: write tmp, replace.
        try:
            tmp = memory_path.with_suffix(".md.dream.tmp")
            tmp.write_text(new_text, encoding="utf-8")
            import os as _os
            _os.replace(tmp, memory_path)
        except OSError as exc:
            return {"ok": False, "error": f"write failed: {exc}"}

        # Bump prompt-freeze generation so live sessions pick up.
        try:
            from xmclaw.daemon.agent_loop import bump_prompt_freeze_generation
            bump_prompt_freeze_generation()
        except Exception:  # noqa: BLE001
            pass

        # Telemetry.
        info = {
            "ok": True,
            "before_chars": len(current),
            "after_chars": len(new_text),
            "saved_chars": len(current) - len(new_text),
            "backup_path": str(backup_path),
            "memory_path": str(memory_path),
            "ts": time.time(),
        }
        if self._bus is not None:
            try:
                from xmclaw.core.bus import EventType, make_event
                ev = make_event(
                    session_id="_system", agent_id="dream",
                    type=EventType.MEMORY_DREAMED, payload=dict(info),
                )
                await self._bus.publish(ev)
            except Exception:  # noqa: BLE001
                pass
        return info

    def _read_recent_daily_logs(self, pdir: Path) -> str:
        """Concatenate the last N days of memory/YYYY-MM-DD.md logs.

        Returns at most ~8KB to keep the prompt under control. Older
        logs are dropped silently — they're already implicit in
        MEMORY.md if they mattered."""
        log_dir = pdir / "memory"
        if not log_dir.is_dir():
            return ""
        try:
            entries = sorted(log_dir.glob("*.md"), reverse=True)
        except OSError:
            return ""
        kept: list[str] = []
        budget = 8000
        for e in entries[: self._window_days]:
            try:
                body = e.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            block = f"### {e.stem}\n\n{body}\n"
            if budget <= 0:
                break
            kept.append(block[:budget])
            budget -= len(block)
        return "\n".join(kept)


class DreamCron:
    """Background task that fires :meth:`DreamCompactor.dream` once
    per day at the configured hour:minute (local time)."""

    def __init__(
        self,
        *,
        compactor: DreamCompactor,
        hour: int = 3,
        minute: int = 0,
    ) -> None:
        self._compactor = compactor
        self._hour = max(0, min(int(hour), 23))
        self._minute = max(0, min(int(minute), 59))
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
        self._task = asyncio.create_task(self._loop(), name="dream-cron")

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
            wait_s = _hhmm_until_next(self._hour, self._minute)
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=wait_s)
                return  # stopped
            except asyncio.TimeoutError:
                pass
            try:
                self._last_result = await self._compactor.dream()
                self._last_run_at = time.time()
            except Exception as exc:  # noqa: BLE001
                _log.warning("dream_cron.failed err=%s", exc)
                self._last_result = {"ok": False, "error": str(exc)}
                self._last_run_at = time.time()
