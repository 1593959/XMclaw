"""xm-auto-evo control surface — status / start / stop / events / genes.

Mounted at ``/api/v2/auto_evo``. Backs the Web UI Evolution page.

Reads from ``app.state.auto_evo_process`` (the managed Node.js
heartbeat) and ``data_dir() / 'auto_evo' / *`` (the JSONL artifacts
xm-auto-evo writes during its observe/learn/evolve cycles).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

from xmclaw.daemon.auto_evo_bridge import auto_evo_workspace

router = APIRouter(prefix="/api/v2/auto_evo", tags=["auto_evo"])


def _read_jsonl(path: Path, *, tail: int = 100) -> list[dict[str, Any]]:
    """Cheap JSONL tail. xm-auto-evo's events.jsonl can grow large
    over time, so we read the whole file but only return the last
    ``tail`` rows. SQLite would be cleaner but we don't control the
    JS side's storage choice."""
    if not path.is_file():
        return []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for ln in lines[-tail:]:
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


@router.get("/status")
async def status(request: Request) -> JSONResponse:
    proc = getattr(request.app.state, "auto_evo_process", None)
    workspace = auto_evo_workspace()
    info: dict[str, Any] = {
        "wired": proc is not None,
        "running": False,
        "pid": None,
        "workspace": str(workspace),
        "log_path": None,
        "counts": {
            "events": 0,
            "genes": 0,
            "capsules": 0,
        },
    }
    if proc is not None:
        info["running"] = proc.is_running
        info["pid"] = proc.pid
        info["log_path"] = str(proc.log_path)

    # File-based counts — xm-auto-evo writes these regardless of
    # whether the heartbeat is running right now.
    events_path = workspace / "events.jsonl"
    if events_path.is_file():
        try:
            with events_path.open("r", encoding="utf-8", errors="replace") as f:
                info["counts"]["events"] = sum(1 for _ in f)
        except OSError:
            pass
    genes_path = workspace / "genes.json"
    if genes_path.is_file():
        try:
            data = json.loads(genes_path.read_text(encoding="utf-8"))
            info["counts"]["genes"] = (
                len(data.get("genes", []))
                if isinstance(data, dict)
                else (len(data) if isinstance(data, list) else 0)
            )
        except (OSError, json.JSONDecodeError):
            pass
    capsules_path = workspace / "capsules.jsonl"
    if capsules_path.is_file():
        try:
            with capsules_path.open("r", encoding="utf-8", errors="replace") as f:
                info["counts"]["capsules"] = sum(1 for _ in f)
        except OSError:
            pass

    return JSONResponse(info)


@router.post("/start")
async def start(request: Request) -> JSONResponse:
    proc = getattr(request.app.state, "auto_evo_process", None)
    if proc is None:
        return JSONResponse(
            {"ok": False, "error": "auto_evo not wired (set evolution.xm_auto_evo.enabled=true)"},
            status_code=400,
        )
    res = await proc.start()
    return JSONResponse(res, status_code=200 if res.get("ok") else 500)


@router.post("/stop")
async def stop(request: Request) -> JSONResponse:
    proc = getattr(request.app.state, "auto_evo_process", None)
    if proc is None:
        return JSONResponse({"ok": False, "error": "auto_evo not wired"}, status_code=400)
    res = await proc.stop()
    return JSONResponse(res)


@router.post("/run/{command}")
async def run_once(command: str, request: Request) -> JSONResponse:
    """Fire a one-shot xm-auto-evo command (start / observe / learn /
    evolve / suggest / status). Synchronous up to 120s."""
    proc = getattr(request.app.state, "auto_evo_process", None)
    if proc is None:
        return JSONResponse({"ok": False, "error": "auto_evo not wired"}, status_code=400)
    if command not in {"start", "observe", "learn", "evolve", "suggest", "status"}:
        return JSONResponse(
            {"ok": False, "error": f"unknown command {command!r}"},
            status_code=400,
        )
    res = await proc.run_once(command)
    return JSONResponse(res)


@router.get("/events")
async def events(tail: int = 100) -> JSONResponse:
    """Tail xm-auto-evo's events.jsonl — observe_complete /
    learn_complete / evolution_complete / solidify_failed etc."""
    workspace = auto_evo_workspace()
    rows = _read_jsonl(workspace / "events.jsonl", tail=max(1, min(int(tail), 1000)))
    return JSONResponse({"events": rows, "count": len(rows)})


@router.get("/genes")
async def genes() -> JSONResponse:
    """Return the current gene catalogue."""
    workspace = auto_evo_workspace()
    path = workspace / "genes.json"
    if not path.is_file():
        return JSONResponse({"genes": []})
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return JSONResponse({"genes": [], "error": str(exc)})
    if isinstance(data, dict):
        return JSONResponse({"genes": data.get("genes", []), **{
            k: v for k, v in data.items() if k != "genes"
        }})
    if isinstance(data, list):
        return JSONResponse({"genes": data})
    return JSONResponse({"genes": []})


@router.get("/capsules")
async def capsules(tail: int = 50) -> JSONResponse:
    """Tail capsules.jsonl — one row per evolution attempt
    (success/fail + what was tried)."""
    workspace = auto_evo_workspace()
    rows = _read_jsonl(
        workspace / "capsules.jsonl", tail=max(1, min(int(tail), 500))
    )
    return JSONResponse({"capsules": rows, "count": len(rows)})


@router.get("/learned_skills")
async def learned_skills() -> JSONResponse:
    """Skills xm-auto-evo has generated AND XMclaw is actively
    feeding into the agent's system prompt.

    This is the closed-loop view: anything listed here is reachable
    by the agent on its next turn. If a skill is on disk but NOT in
    this list, something's wrong with the loader (perhaps the
    SKILL.md is malformed)."""
    from xmclaw.daemon.learned_skills import default_learned_skills_loader
    loader = default_learned_skills_loader()
    return JSONResponse({
        "skills_root": str(loader.skills_root),
        "skills": loader.list_for_api(),
    })


@router.get("/log")
async def log(lines: int = 200) -> JSONResponse:
    """Tail the heartbeat log for the UI."""
    workspace = auto_evo_workspace()
    log_path = workspace / "auto_evo.log"
    if not log_path.is_file():
        return JSONResponse({"lines": [], "exists": False, "path": str(log_path)})
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
    except OSError as exc:
        return JSONResponse({"lines": [], "error": str(exc)})
    n = max(1, min(int(lines), 2000))
    return JSONResponse({
        "lines": [ln.rstrip("\n") for ln in all_lines[-n:]],
        "count": len(all_lines),
        "path": str(log_path),
        "exists": True,
    })
