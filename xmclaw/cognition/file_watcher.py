"""FileWatcher — 文件系统感知。

监控工作目录变化，产生感知事件。
Phase 1: 只记录，不主动打扰。
Phase 2: 上下文相关性判断后主动提示。
"""
from __future__ import annotations

import asyncio
import fnmatch
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from xmclaw.utils.log import get_logger

log = get_logger(__name__)

PerceptionEventType = Literal["created", "modified", "deleted", "moved"]


@dataclass(frozen=True, slots=True)
class FilePercept:
    """文件感知事件。"""

    path: str
    event_type: PerceptionEventType
    timestamp: float
    is_directory: bool = False
    src_path: str | None = None  # for "moved"


class FileWatcher:
    """文件系统监控。"""

    def __init__(
        self,
        *,
        watch_paths: list[str],
        ignore_patterns: list[str] | None = None,
        callback: Callable[[FilePercept], Awaitable[None]] | None = None,
        bus: Any | None = None,
        cognitive_state: Any | None = None,
    ) -> None:
        self.watch_paths = [Path(p).expanduser() for p in watch_paths]
        self.ignore_patterns = ignore_patterns or [
            ".git", "__pycache__", ".xmclaw",
            "node_modules", ".venv", "*.pyc", ".ruff_cache",
            ".mypy_cache", ".pytest_cache",
        ]
        self.callback = callback
        self._bus = bus
        self._cognitive_state = cognitive_state
        self._running = False
        self._task: asyncio.Task[Any] | None = None
        self._last_snapshot: dict[str, float] = {}

    def _should_ignore(self, path: str) -> bool:
        """检查路径是否应被忽略。

        匹配规则（修复 2026-05-10 bug：``.git`` 内部所有文件都泄漏到
        attention focus —— 因为 ``fnmatch.fnmatch(完整路径, ".git")``
        永远 False，fnmatch 不做子串匹配）：

        1. **路径任意一段** 匹配 pattern → 忽略。这处理 ``.git`` /
           ``__pycache__`` / ``.xmclaw`` 等出现在中间层的目录。
        2. **basename** 匹配 pattern → 忽略。这处理 ``*.pyc`` /
           ``*.tmp`` 等通配符。
        """
        p = Path(path)
        for pattern in self.ignore_patterns:
            # Rule 1: any path segment matches the pattern.
            # Path("C:/.../XMclaw/.git/logs/.../main").parts contains
            # ".git" → fnmatch(".git", ".git") = True → ignore.
            if any(fnmatch.fnmatch(part, pattern) for part in p.parts):
                return True
            # Rule 2: basename matches (covers wildcards like *.pyc).
            if fnmatch.fnmatch(p.name, pattern):
                return True
        return False

    async def start(self) -> None:
        """启动监控。"""
        self._running = True
        # Phase 1: 使用轮询方案（watchdog 为可选增强）
        self._task = asyncio.create_task(self._poll_loop(), name="file-watcher")

    async def stop(self) -> None:
        """停止监控。"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _poll_loop(self) -> None:
        """轮询循环。"""
        # 初始化快照
        self._last_snapshot = self._take_snapshot()

        while self._running:
            await asyncio.sleep(5.0)  # 5 秒轮询间隔
            if not self._running:
                break

            new_snapshot = self._take_snapshot()
            events = self._diff_snapshots(self._last_snapshot, new_snapshot)
            self._last_snapshot = new_snapshot

            for event in events:
                # Publish to event bus for downstream subscribers.
                if self._bus is not None:
                    try:
                        from xmclaw.core.bus import EventType, make_event
                        ev = make_event(
                            session_id="_system",
                            agent_id="cognition",
                            type=EventType.FILE_SYSTEM_EVENT,
                            payload={
                                "path": event.path,
                                "event_type": event.event_type,
                                "is_directory": event.is_directory,
                            },
                        )
                        asyncio.create_task(self._bus.publish(ev))
                    except Exception:
                        log.warning("file_watcher.publish_failed", exc_info=True)
                # Register as attention focus when cognitive state is wired.
                if self._cognitive_state is not None:
                    try:
                        salience = await self._cognitive_state.compute_salience(
                            percept_id=f"fs:{event.path}",
                            content=f"[{event.event_type}] {event.path}",
                            urgency=0.4,
                            relevance=0.3,
                            novelty=0.8,
                        )
                        if salience >= self._cognitive_state.salience_threshold:
                            from xmclaw.cognition.state import AttentionFocus
                            self._cognitive_state.add_focus(
                                AttentionFocus(
                                    percept_id=f"fs:{event.path}",
                                    content=f"[{event.event_type}] {event.path}",
                                    salience_score=salience,
                                )
                            )
                    except Exception:
                        log.warning("file_watcher.salience_failed", exc_info=True)
                if self.callback:
                    try:
                        await self.callback(event)
                    except Exception:
                        log.warning("file_watcher.callback_failed", exc_info=True)

    def _take_snapshot(self) -> dict[str, float]:
        """拍摄文件系统快照。"""
        snapshot: dict[str, float] = {}
        for watch_path in self.watch_paths:
            if not watch_path.exists():
                continue
            try:
                for item in watch_path.rglob("*"):
                    if self._should_ignore(str(item)):
                        continue
                    try:
                        stat = item.stat()
                        snapshot[str(item)] = stat.st_mtime
                    except (OSError, PermissionError):
                        continue
            except (OSError, PermissionError):
                continue
        return snapshot

    def _diff_snapshots(
        self,
        old: dict[str, float],
        new: dict[str, float],
    ) -> list[FilePercept]:
        """对比快照，生成感知事件。"""
        events: list[FilePercept] = []
        now = time.time()

        # 新增 / 修改
        for path, mtime in new.items():
            if path not in old:
                events.append(FilePercept(path, "created", now))
            elif old[path] != mtime:
                events.append(FilePercept(path, "modified", now))

        # 删除
        for path in old:
            if path not in new:
                events.append(FilePercept(path, "deleted", now))

        return events

    async def is_contextually_relevant(self, path: str) -> bool:
        """判断文件变化是否与当前上下文相关。
        Phase 1: 始终返回 False（只记录不打扰）。
        Phase 2: 基于最近 memory search 的 query 与路径的 token 重叠判断。
        """
        # Phase 1: 不打扰
        return False
