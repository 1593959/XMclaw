"""Shared helpers for reading and writing the daemon config file."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def resolve_config_file(raw: str | Path | None) -> Path | None:
    """Return the concrete ``config.json`` file path for daemon writes.

    Older UI routes accepted ``app.state.config_path`` verbatim. If the
    daemon was started with ``--config daemon`` (or another directory), those
    routes tried to replace the directory itself, which fails on Windows with
    ``[WinError 5] Access is denied: 'daemon'``. Directory-like paths now
    consistently resolve to ``<dir>/config.json``.
    """
    if raw is None:
        fallback = Path("daemon") / "config.json"
        return fallback if fallback.exists() else None

    path = Path(raw)
    if path.exists() and path.is_dir():
        return path / "config.json"
    if path.suffix == "" and path.name.lower() in {"daemon", ".xmclaw", "config"}:
        return path / "config.json"
    return path


def request_config_file(request: Any) -> Path | None:
    return resolve_config_file(getattr(request.app.state, "config_path", None))


def load_config_file(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise ValueError(f"existing config is invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        return {}
    return data


def write_config_file(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)


def replace_runtime_config(request: Any, data: dict[str, Any]) -> None:
    current = getattr(request.app.state, "config", None)
    if isinstance(current, dict):
        current.clear()
        current.update(data)
    else:
        request.app.state.config = data
