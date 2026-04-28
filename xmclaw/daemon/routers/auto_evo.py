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


def _read_jsonl(path: Path | None, *, tail: int = 100) -> list[dict[str, Any]]:
    """Cheap JSONL tail. xm-auto-evo's events.jsonl can grow large
    over time, so we read the whole file but only return the last
    ``tail`` rows. SQLite would be cleaner but we don't control the
    JS side's storage choice."""
    if path is None or not path.is_file():
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
    #
    # B-22 path fix: xm-auto-evo writes everything under
    # ``<workspace>/data/`` (gep/store.js DATA_DIR), NOT directly
    # under workspace. The earlier ``workspace / "events.jsonl"``
    # path always missed and reported 0/0/0 even after evolution
    # had genuinely produced output. Try both locations to stay
    # compatible with possible future layouts.
    def _resolve_first(*candidates: Path) -> Path | None:
        for c in candidates:
            if c.is_file():
                return c
        return None

    events_path = _resolve_first(
        workspace / "data" / "events.jsonl",
        workspace / "events.jsonl",
    )
    if events_path is not None:
        try:
            with events_path.open("r", encoding="utf-8", errors="replace") as f:
                info["counts"]["events"] = sum(1 for _ in f)
        except OSError:
            pass

    genes_path = _resolve_first(
        workspace / "data" / "genes.json",
        workspace / "genes.json",
    )
    if genes_path is not None:
        try:
            data = json.loads(genes_path.read_text(encoding="utf-8"))
            info["counts"]["genes"] = (
                len(data.get("genes", []))
                if isinstance(data, dict)
                else (len(data) if isinstance(data, list) else 0)
            )
        except (OSError, json.JSONDecodeError):
            pass

    capsules_path = _resolve_first(
        workspace / "data" / "capsules.jsonl",
        workspace / "data" / "capsules.json",
        workspace / "capsules.jsonl",
    )
    if capsules_path is not None:
        try:
            with capsules_path.open("r", encoding="utf-8", errors="replace") as f:
                # A .json file holds an array, .jsonl is one row per line.
                if capsules_path.suffix == ".json":
                    data = json.loads(f.read())
                    info["counts"]["capsules"] = (
                        len(data) if isinstance(data, list) else len(data.get("capsules", []))
                    )
                else:
                    info["counts"]["capsules"] = sum(1 for _ in f)
        except (OSError, json.JSONDecodeError):
            pass

    # Also count auto-generated skills on disk — those are the closed-
    # loop USABLE products. /skills_count is what matters for "did
    # evolution actually produce something the agent can use?"
    skills_dir = workspace / "skills"
    skills_count = 0
    if skills_dir.is_dir():
        for entry in skills_dir.iterdir():
            if entry.is_dir() and (entry / "SKILL.md").is_file():
                skills_count += 1
    info["counts"]["learned_skills"] = skills_count

    return JSONResponse(info)


@router.post("/start")
async def start(request: Request) -> JSONResponse:
    proc = getattr(request.app.state, "auto_evo_process", None)
    if proc is None:
        return JSONResponse(
            {"ok": False, "error": "auto_evo not wired (set evolution.auto_evo.enabled=true)"},
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


def _resolve_data_file(workspace: Path, *names: str) -> Path | None:
    """Probe both ``<workspace>/data/<name>`` and ``<workspace>/<name>``
    for each candidate name. xm-auto-evo's gep/store.js writes under
    the data/ subdir — earlier router code always missed that."""
    for n in names:
        for base in (workspace / "data", workspace):
            p = base / n
            if p.is_file():
                return p
    return None


@router.get("/events")
async def events(tail: int = 100) -> JSONResponse:
    """Tail xm-auto-evo's events.jsonl — observe_complete /
    learn_complete / evolution_complete / solidify_failed etc."""
    workspace = auto_evo_workspace()
    path = _resolve_data_file(workspace, "events.jsonl")
    rows = _read_jsonl(path, tail=max(1, min(int(tail), 1000))) if path else []
    return JSONResponse({"events": rows, "count": len(rows)})


@router.get("/genes")
async def genes() -> JSONResponse:
    """Return the current gene catalogue."""
    workspace = auto_evo_workspace()
    path = _resolve_data_file(workspace, "genes.json")
    if path is None:
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
    """Tail capsules — one row per evolution attempt (success/fail +
    what was tried). xm-auto-evo writes capsules.json (array) on
    some paths and capsules.jsonl on others; we try both."""
    workspace = auto_evo_workspace()
    n = max(1, min(int(tail), 500))
    # JSONL preferred
    path = _resolve_data_file(workspace, "capsules.jsonl")
    if path is not None:
        rows = _read_jsonl(path, tail=n)
        return JSONResponse({"capsules": rows, "count": len(rows)})
    # JSON fallback
    path = _resolve_data_file(workspace, "capsules.json")
    if path is not None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                rows = data[-n:]
            elif isinstance(data, dict):
                rows = (data.get("capsules") or [])[-n:]
            else:
                rows = []
            return JSONResponse({"capsules": rows, "count": len(rows)})
        except (OSError, json.JSONDecodeError):
            pass
    return JSONResponse({"capsules": [], "count": 0})


@router.get("/learned_skills")
async def learned_skills(include_disabled: bool = False) -> JSONResponse:
    """Skills xm-auto-evo has generated AND XMclaw is actively
    feeding into the agent's system prompt.

    B-29: each skill now also carries an ``invocation_count`` —
    aggregated from SKILL_INVOKED events on the bus. Lets the UI
    show per-skill usage so auto_repair_v9 can be compared with v8
    by REAL invocations not just by version number."""
    from xmclaw.daemon.learned_skills import default_learned_skills_loader
    loader = default_learned_skills_loader()
    skills = loader.list_for_api(include_disabled=include_disabled)

    # Aggregate skill invocation counts + B-35 outcome verdicts from
    # the events DB. One DB pass for both event types — a single scan
    # of the last N events covers invocation_count, success_count,
    # error_count, partial_count.
    invocation_counts: dict[str, int] = {}
    verdict_counts: dict[str, dict[str, int]] = {}
    last_fired: dict[str, float] = {}
    try:
        import sqlite3
        from xmclaw.utils.paths import data_dir
        db = data_dir() / "v2" / "events.db"
        if db.is_file():
            con = sqlite3.connect(str(db))
            con.row_factory = sqlite3.Row
            try:
                rows = con.execute(
                    "SELECT type, ts, payload FROM events "
                    "WHERE type IN ('skill_invoked', 'skill_outcome') "
                    "ORDER BY ts DESC LIMIT 2000"
                ).fetchall()
                for r in rows:
                    try:
                        p = json.loads(r["payload"]) if r["payload"] else {}
                    except (ValueError, TypeError):
                        continue
                    sid = p.get("skill_id")
                    if not sid:
                        continue
                    if r["type"] == "skill_invoked":
                        invocation_counts[sid] = invocation_counts.get(sid, 0) + 1
                        # Rows are DESC by ts so first hit per sid is newest.
                        last_fired.setdefault(sid, float(r["ts"]))
                    else:  # skill_outcome
                        verdict = str(p.get("verdict") or "")
                        if verdict not in ("success", "partial", "error"):
                            continue
                        d = verdict_counts.setdefault(
                            sid, {"success": 0, "partial": 0, "error": 0},
                        )
                        d[verdict] += 1
            finally:
                con.close()
    except Exception:  # noqa: BLE001
        pass

    for s in skills:
        s["invocation_count"] = invocation_counts.get(s["skill_id"], 0)
        s["outcomes"] = verdict_counts.get(
            s["skill_id"], {"success": 0, "partial": 0, "error": 0},
        )
        s["last_fired_ts"] = last_fired.get(s["skill_id"])

    return JSONResponse({
        "skills_root": str(loader.skills_root),
        "skills": skills,
    })


@router.post("/learned_skills/{skill_id}/disable")
async def disable_learned_skill(skill_id: str, request: Request) -> JSONResponse:
    """B-33: park or unpark a learned skill without deleting it.

    Writes ``disabled: true`` (or removes it) into the SKILL.md
    frontmatter, then bumps the prompt-freeze generation so every
    running session reflects the change on its next turn.

    Body: ``{"disabled": true}`` to park, ``{"disabled": false}`` to
    re-enable. Defaults to true (the common case is "stop this thing
    misfiring NOW"). Returns the new disabled state.
    """
    from xmclaw.daemon.learned_skills import default_learned_skills_loader
    from xmclaw.daemon.agent_loop import bump_prompt_freeze_generation

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        body = {}
    if not isinstance(body, dict):
        body = {}
    disable = bool(body.get("disabled", True))

    loader = default_learned_skills_loader()
    # Path-traversal hardening: normalise + reject anything that
    # tries to escape the skills root.
    safe_id = skill_id.replace("\\", "/").split("/")[-1].strip()
    if not safe_id or safe_id.startswith("."):
        return JSONResponse(
            {"ok": False, "error": "invalid skill_id"}, status_code=400,
        )
    skill_dir = loader.skills_root / safe_id
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return JSONResponse(
            {"ok": False, "error": f"skill {safe_id!r} not found"},
            status_code=404,
        )

    text = skill_md.read_text(encoding="utf-8", errors="replace")
    new_text = _set_frontmatter_key(text, "disabled", "true" if disable else None)
    if new_text != text:
        skill_md.write_text(new_text, encoding="utf-8")

    # Drop loader cache + bump generation so live sessions pick up.
    loader._cache_key = None  # type: ignore[attr-defined]
    bump_prompt_freeze_generation()
    return JSONResponse({
        "ok": True,
        "skill_id": safe_id,
        "disabled": disable,
        "path": str(skill_md),
    })


def _set_frontmatter_key(text: str, key: str, value: str | None) -> str:
    """Insert / update / remove a single frontmatter key.

    Lightweight YAML-frontmatter mutator — paired with the lightweight
    parser in :mod:`xmclaw.daemon.learned_skills` (we deliberately
    don't pull PyYAML for either side).

    * ``value`` non-None → set ``key: value``
    * ``value`` None → remove the line entirely
    * No frontmatter block → wrap the body in one when setting; no-op
      when removing.
    """
    lines = text.splitlines(keepends=True)
    if lines and lines[0].rstrip("\r\n") == "---":
        # Find closing fence
        end_idx = None
        for i in range(1, len(lines)):
            if lines[i].rstrip("\r\n") == "---":
                end_idx = i
                break
        if end_idx is None:
            # Malformed; bail out unchanged.
            return text
        fm_block = lines[1:end_idx]
        # Locate existing key (top-level inline form only).
        key_idx = None
        for i, ln in enumerate(fm_block):
            stripped = ln.lstrip()
            if stripped.startswith(f"{key}:") or stripped.rstrip() == f"{key}:":
                key_idx = i
                break
        if value is None:
            if key_idx is not None:
                del fm_block[key_idx]
        else:
            new_line = f"{key}: {value}\n"
            if key_idx is not None:
                fm_block[key_idx] = new_line
            else:
                fm_block.append(new_line)
        return "".join(lines[:1] + fm_block + lines[end_idx:])
    # No frontmatter — wrap the file when setting.
    if value is None:
        return text
    return f"---\n{key}: {value}\n---\n{text}"


@router.post("/learned_skills/reload")
async def reload_learned_skills() -> JSONResponse:
    """B-32: force a rescan of the learned-skills directory + bump
    the prompt-freeze generation so every running session picks up
    the new set on its next turn.

    Normally ``LearnedSkillsLoader.render_section`` auto-bumps when
    the fingerprint changes — this endpoint is the manual override
    for "I just dropped a SKILL.md in by hand, make the agent see
    it RIGHT NOW" and for tests/CI that need deterministic flushes.
    Returns the post-reload skill count.
    """
    from xmclaw.daemon.learned_skills import default_learned_skills_loader
    from xmclaw.daemon.agent_loop import bump_prompt_freeze_generation

    loader = default_learned_skills_loader()
    # Drop the loader's own fingerprint cache so the next render
    # actually re-scans the disk + re-builds the markdown block.
    loader._cache_key = None  # type: ignore[attr-defined]
    skills = loader.list_skills()
    bump_prompt_freeze_generation()
    return JSONResponse({
        "ok": True,
        "skills_count": len(skills),
        "skills_root": str(loader.skills_root),
        "bumped": True,
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
