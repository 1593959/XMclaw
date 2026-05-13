"""Persistent registry of proactive-push targets — Wave 10.

The Wave 9 bridge originally only knew about targets declared in
``config.channels.<id>.proactive_chat_id``. Users who wanted to opt-in
had to edit JSON + restart daemon.

This store lets a user run ``/订阅`` from their feishu chat and the
bridge picks the target up on the next event — and remembers it
across daemon restarts via ``~/.xmclaw/v2/proactive_targets.json``.

File format (atomic write, tolerant read):

  {
    "version": 1,
    "targets": [
      { "channel": "feishu", "ref": "oc_xxxx", "added_ts": 1234567890.0 }
    ]
  }

Corruption / missing file = treat as empty (no crash). Concurrent
writes are serialized via a module-level asyncio.Lock; the daemon is
single-process so this is sufficient.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from xmclaw.utils.log import get_logger
from xmclaw.utils.paths import data_dir

logger = get_logger(__name__)

_STORE_VERSION = 1
_WRITE_LOCK = asyncio.Lock()


@dataclass(frozen=True, slots=True)
class ProactiveTarget:
    channel: str
    ref: str
    added_ts: float


def _default_path() -> Path:
    return data_dir() / "v2" / "proactive_targets.json"


def load_targets(path: Path | None = None) -> list[ProactiveTarget]:
    """Read targets from disk. Empty list on missing/corrupted file."""
    p = path or _default_path()
    try:
        if not p.exists():
            return []
        raw = p.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "proactive_target_store.load_failed path=%s err=%s",
            p, exc,
        )
        return []
    if not isinstance(data, dict):
        return []
    items = data.get("targets")
    if not isinstance(items, list):
        return []
    out: list[ProactiveTarget] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        ch = it.get("channel")
        ref = it.get("ref")
        if not isinstance(ch, str) or not isinstance(ref, str) or not ref.strip():
            continue
        out.append(ProactiveTarget(
            channel=ch.strip(),
            ref=ref.strip(),
            added_ts=float(it.get("added_ts") or 0.0),
        ))
    return out


async def add_target(
    channel: str, ref: str, *, path: Path | None = None,
) -> bool:
    """Add a target. Returns True if newly added, False if already present."""
    if not channel or not ref or not isinstance(ref, str) or not ref.strip():
        return False
    p = path or _default_path()
    async with _WRITE_LOCK:
        targets = load_targets(p)
        for t in targets:
            if t.channel == channel and t.ref == ref:
                return False
        targets.append(ProactiveTarget(
            channel=channel, ref=ref.strip(), added_ts=time.time(),
        ))
        _atomic_write(p, targets)
        logger.info(
            "proactive_target_store.added channel=%s ref=%s",
            channel, ref,
        )
        return True


async def remove_target(
    channel: str, ref: str, *, path: Path | None = None,
) -> bool:
    """Remove a target. Returns True if it was present."""
    p = path or _default_path()
    async with _WRITE_LOCK:
        targets = load_targets(p)
        before = len(targets)
        targets = [
            t for t in targets
            if not (t.channel == channel and t.ref == ref)
        ]
        if len(targets) == before:
            return False
        _atomic_write(p, targets)
        logger.info(
            "proactive_target_store.removed channel=%s ref=%s",
            channel, ref,
        )
        return True


def _atomic_write(
    p: Path, targets: list[ProactiveTarget],
) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "version": _STORE_VERSION,
        "targets": [asdict(t) for t in targets],
    }
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(p)


__all__ = [
    "ProactiveTarget",
    "load_targets",
    "add_target",
    "remove_target",
]
