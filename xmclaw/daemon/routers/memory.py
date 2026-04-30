"""Memory Editor API — read/write/search user-authored markdown notes.

Epic #18 Phase A. Mounted at ``/api/v2/memory``. Backs the web-UI
"memory editor" panel: read the list of notes, load one, save it
back, search across them.

Storage: plain markdown files under
:func:`xmclaw.utils.paths.file_memory_dir` (``~/.xmclaw/memory/``).

Distinct from the SQLite-vec long-term memory at
:func:`xmclaw.utils.paths.default_memory_db_path` — that one is
daemon-managed with embeddings; this one is human-authored notes the
user wants to persist and revisit.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

from xmclaw.utils.paths import file_memory_dir

router = APIRouter(prefix="/api/v2/memory", tags=["memory"])


def _safe_name(filename: str) -> str:
    """Collapse any path traversal to a bare filename and enforce ``.md``.

    ``Path(filename).name`` strips any leading dirs. On POSIX it only
    treats ``/`` as a separator, so a caller passing a Windows-style
    ``..\\evil`` slips through. Normalize backslashes to forward before
    ``.name`` so a Linux daemon can't be tricked by a Windows client
    (or vice-versa — belt and braces).

    Empty input after stripping becomes ``"note.md"`` so we never
    create a bare ``.md`` file with no stem.
    """
    normalized = filename.replace("\\", "/")
    stem = Path(normalized).name.strip()
    if not stem:
        return "note.md"
    if not stem.endswith(".md"):
        stem = f"{stem}.md"
    return stem


_AVAILABLE_PROVIDERS = {
    "sqlite_vec": {
        "label": "SQLite Vec (built-in vector store)",
        "kind": "external",
        "needs": [],
        "description": "Local vector DB — no external service. Good default.",
    },
    "hindsight": {
        "label": "Hindsight (cloud knowledge graph)",
        "kind": "external",
        "needs": ["evolution.memory.hindsight.api_key"],
        "description": "Knowledge-graph backed long-term memory. Needs API key.",
    },
    "supermemory": {
        "label": "Supermemory (cloud key-value memory)",
        "kind": "external",
        "needs": ["evolution.memory.supermemory.api_key"],
        "description": "Cloud key-value memory store. Needs API key.",
    },
    "mem0": {
        "label": "Mem0 (cloud agent memory)",
        "kind": "external",
        "needs": ["evolution.memory.mem0.api_key"],
        "description": "Mem0.ai cloud agent memory. Needs API key.",
    },
    "none": {
        "label": "Disabled (no external provider)",
        "kind": "external",
        "needs": [],
        "description": "Only the always-on builtin file provider runs.",
    },
}


@router.get("/providers/available")
async def list_available_providers() -> JSONResponse:
    """Catalogue of provider implementations the user can switch to.

    The 'active' one is whichever the running agent has registered as
    its external provider (per /providers); switching writes config
    and requires a daemon restart to take effect.
    """
    return JSONResponse({
        "providers": [
            {"id": pid, **meta}
            for pid, meta in _AVAILABLE_PROVIDERS.items()
        ],
    })


@router.get("/relevant_picker/status")
async def relevant_picker_status(request: Request) -> JSONResponse:
    """B-96: surface the current LLM-pick top-K relevant-files setting
    + whether the running daemon picked it up.

    Two layers:
      * config (on disk)  ←  what's saved
      * runtime           ←  what the AgentLoop actually reads
    Mismatch → user edited config but didn't restart, same pattern
    as embedding (B-87).
    """
    state = request.app.state
    cfg = getattr(state, "config", None) or {}
    section = (
        ((cfg.get("evolution") or {}).get("memory") or {}).get("relevant_picker") or {}
    )
    cfg_enabled = bool(section.get("enabled", False))
    cfg_k = int(section.get("k", 3))
    cfg_max_chars = int(section.get("max_chars", 4000))

    agent = getattr(state, "agent", None)
    runtime_enabled = bool(getattr(agent, "_relevant_files_picker_enabled", False))
    runtime_k = int(getattr(agent, "_relevant_files_picker_k", 3))
    runtime_max_chars = int(getattr(agent, "_relevant_files_max_chars", 4000))

    return JSONResponse({
        "config": {
            "enabled": cfg_enabled,
            "k": cfg_k,
            "max_chars": cfg_max_chars,
        },
        "runtime": {
            "enabled": runtime_enabled,
            "k": runtime_k,
            "max_chars": runtime_max_chars,
        },
        "restart_pending": (
            cfg_enabled != runtime_enabled
            or cfg_k != runtime_k
            or cfg_max_chars != runtime_max_chars
        ),
    })


@router.post("/relevant_picker/configure")
async def configure_relevant_picker(request: Request) -> JSONResponse:
    """B-96: write the ``evolution.memory.relevant_picker`` config
    section. Body: ``{enabled: bool, k: int?, max_chars: int?}``.

    Same shape as ``/embedding/configure`` (B-76) — daemon restart
    still required to actually start using the new value (the picker
    flag is read once in ``factory.build_agent_from_config``).
    """
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)

    enabled = bool(body.get("enabled", False))
    try:
        k = int(body.get("k", 3))
    except (TypeError, ValueError):
        k = 3
    try:
        max_chars = int(body.get("max_chars", 4000))
    except (TypeError, ValueError):
        max_chars = 4000
    if k < 1 or k > 20:
        return JSONResponse(
            {"ok": False, "error": "k must be in [1, 20]"}, status_code=400,
        )
    if max_chars < 500 or max_chars > 50000:
        return JSONResponse(
            {"ok": False, "error": "max_chars must be in [500, 50000]"},
            status_code=400,
        )

    state = request.app.state
    cfg = getattr(state, "config", None)
    if cfg is None:
        return JSONResponse(
            {"ok": False, "error": "no config attached to daemon"}, status_code=500,
        )
    cfg.setdefault("evolution", {}).setdefault("memory", {})["relevant_picker"] = {
        "enabled": enabled, "k": k, "max_chars": max_chars,
    }
    config_path = getattr(state, "config_path", None)
    if config_path:
        try:
            from pathlib import Path as _P
            from xmclaw.utils.fs_locks import atomic_write_text
            p = _P(config_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(p, json.dumps(cfg, indent=2, ensure_ascii=False))
        except OSError as exc:
            return JSONResponse(
                {"ok": False, "error": f"config write failed: {exc}"},
                status_code=500,
            )
    return JSONResponse({
        "ok": True,
        "settings": {"enabled": enabled, "k": k, "max_chars": max_chars},
        "restart_required": True,
        "config_path": str(config_path) if config_path else None,
    })


@router.post("/embedding/configure")
async def configure_embedding(request: Request) -> JSONResponse:
    """B-76: write the ``evolution.memory.embedding`` config section
    from the Web UI.

    Body fields:
      * ``provider``    str — currently ``"openai"`` (covers OpenAI,
                        Ollama, vLLM, DashScope; build_embedding_provider
                        normalises non-openai shapes to the openai
                        client). Required.
      * ``base_url``    str — e.g. ``http://127.0.0.1:11434/v1`` for a
                        local Ollama. Optional (defaults to
                        ``https://api.openai.com/v1``).
      * ``model``       str — model id. Required.
      * ``dimensions``  int — vector dim, must match what the model
                        actually emits or the indexer raises mid-batch.
                        Required (no sensible default — wrong dim
                        silently corrupts the vec table).
      * ``api_key``     str | empty — auth key. Optional for localhost
                        endpoints (B-43).
      * ``timeout_s``, ``max_batch_size`` — optional, sane defaults.

    Same shape as ``/providers/switch``: writes config to disk and
    updates in-memory config; daemon restart still required to actually
    rebuild the embedder + start the indexer (start is wired in the
    lifespan, not on config change).
    """
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)

    provider = str(body.get("provider", "")).strip().lower() or "openai"
    model = str(body.get("model", "")).strip()
    if not model:
        return JSONResponse(
            {"ok": False, "error": "missing 'model'"}, status_code=400,
        )
    try:
        dimensions = int(body.get("dimensions") or 0)
    except (TypeError, ValueError):
        dimensions = 0
    if dimensions <= 0:
        return JSONResponse(
            {"ok": False, "error": "dimensions must be a positive integer"},
            status_code=400,
        )

    section: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "dimensions": dimensions,
    }
    base_url = str(body.get("base_url", "")).strip()
    if base_url:
        section["base_url"] = base_url
    api_key = str(body.get("api_key", "")).strip()
    if api_key:
        section["api_key"] = api_key
    if "timeout_s" in body:
        try:
            section["timeout_s"] = float(body["timeout_s"])
        except (TypeError, ValueError):
            pass
    if "max_batch_size" in body:
        try:
            section["max_batch_size"] = int(body["max_batch_size"])
        except (TypeError, ValueError):
            pass

    state = request.app.state
    cfg = getattr(state, "config", None)
    if cfg is None:
        return JSONResponse(
            {"ok": False, "error": "no config attached to daemon"}, status_code=500,
        )
    config_path = getattr(state, "config_path", None)
    cfg.setdefault("evolution", {}).setdefault("memory", {})["embedding"] = section

    if config_path:
        try:
            from pathlib import Path as _P
            from xmclaw.utils.fs_locks import atomic_write_text
            p = _P(config_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(p, json.dumps(cfg, indent=2, ensure_ascii=False))
        except OSError as exc:
            return JSONResponse(
                {"ok": False, "error": f"config write failed: {exc}"},
                status_code=500,
            )

    return JSONResponse({
        "ok": True,
        "embedding": section,
        "restart_required": True,
        "config_path": str(config_path) if config_path else None,
    })


@router.post("/providers/switch")
async def switch_provider(request: Request) -> JSONResponse:
    """Switch the external memory provider. Persists to config.

    Body: ``{"provider": "sqlite_vec" | "hindsight" | "none"}``.
    Daemon restart required for the swap to take effect — the
    response includes ``restart_required: true`` so the UI can prompt.
    """
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    provider = str(body.get("provider", "")).strip().lower()
    if provider not in _AVAILABLE_PROVIDERS:
        return JSONResponse(
            {"ok": False, "error": f"unknown provider {provider!r}"},
            status_code=400,
        )

    # Update the running config + persist to disk.
    state = request.app.state
    cfg = getattr(state, "config", None)
    if cfg is None:
        return JSONResponse(
            {"ok": False, "error": "no config attached to daemon"}, status_code=500,
        )
    config_path = getattr(state, "config_path", None)
    cfg.setdefault("evolution", {}).setdefault("memory", {})["provider"] = provider
    if config_path:
        try:
            from pathlib import Path as _P
            from xmclaw.utils.fs_locks import atomic_write_text
            p = _P(config_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            # B-74: atomic write so a crash mid-save doesn't leave the
            # daemon's config truncated (which would prevent the next
            # restart from loading anything).
            atomic_write_text(p, json.dumps(cfg, indent=2, ensure_ascii=False))
        except OSError as exc:
            return JSONResponse(
                {"ok": False, "error": f"config write failed: {exc}"},
                status_code=500,
            )

    return JSONResponse({
        "ok": True,
        "provider": provider,
        "restart_required": True,
        "config_path": str(config_path) if config_path else None,
    })


@router.get("/dream/status")
async def dream_status(request: Request) -> JSONResponse:
    """B-51: surface DreamCron state for the Memory page.

    Returns ``{wired, running, hour, minute, last_run_at, last_result}``.
    ``wired=False`` when no LLM is configured (compactor was never built).
    """
    cron = getattr(request.app.state, "dream_cron", None)
    if cron is None:
        return JSONResponse({
            "wired": False,
            "reason": "no LLM configured (dream needs a complete-able LLM)",
        })
    return JSONResponse({
        "wired": True,
        "running": cron.is_running,
        "hour": cron._hour,           # noqa: SLF001
        "minute": cron._minute,       # noqa: SLF001
        "last_run_at": cron.last_run_at,
        "last_result": cron.last_result,
    })


@router.get("/dream/backups")
async def dream_backups(request: Request) -> JSONResponse:
    """B-52: list MEMORY.md backups created by Auto-Dream / manual run.

    Persona-dir's ``backup/`` subdirectory holds ``memory_backup_*.md``
    files. We return them newest-first with size + ts so the UI / agent
    can choose what to restore from.
    """
    try:
        from xmclaw.daemon.factory import _resolve_persona_profile_dir
        cfg = getattr(request.app.state, "config", None) or {}
        pdir = _resolve_persona_profile_dir(cfg)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"backups": [], "error": str(exc)})
    bdir = pdir / "backup"
    if not bdir.is_dir():
        return JSONResponse({"backups": [], "persona_dir": str(pdir)})
    out: list[dict[str, Any]] = []
    for entry in sorted(bdir.glob("memory_backup_*.md"), reverse=True):
        try:
            stat = entry.stat()
        except OSError:
            continue
        out.append({
            "name": entry.name,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
        })
    return JSONResponse({"backups": out, "persona_dir": str(pdir)})


@router.post("/dream/restore/{name}")
async def dream_restore(name: str, request: Request) -> JSONResponse:
    """B-52: restore MEMORY.md from a named backup.

    Path-traversal hardened: only basenames matching
    ``memory_backup_*.md`` are honoured. The current MEMORY.md is
    backed up to ``memory_backup_predates_<ts>_restore.md`` BEFORE
    overwrite — restores are themselves reversible.
    """
    safe = name.replace("\\", "/").split("/")[-1].strip()
    if not safe.startswith("memory_backup_") or not safe.endswith(".md"):
        return JSONResponse(
            {"ok": False, "error": "invalid backup name"},
            status_code=400,
        )
    try:
        from xmclaw.daemon.factory import _resolve_persona_profile_dir
        cfg = getattr(request.app.state, "config", None) or {}
        pdir = _resolve_persona_profile_dir(cfg)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            {"ok": False, "error": str(exc)}, status_code=500,
        )
    src = pdir / "backup" / safe
    if not src.is_file():
        return JSONResponse(
            {"ok": False, "error": f"backup not found: {safe}"},
            status_code=404,
        )
    target = pdir / "MEMORY.md"
    # Backup current before overwrite (restore is reversible).
    import time as _t
    pre_backup = pdir / "backup" / (
        f"memory_backup_predates_{_t.strftime('%Y%m%d-%H%M%S')}_restore.md"
    )
    try:
        # B-74: atomic writes for both the pre-restore backup and the
        # restored MEMORY.md. A crash mid-restore would otherwise leave
        # MEMORY.md truncated — the very file the user trusts to keep
        # the agent's long-term memory.
        from xmclaw.utils.fs_locks import atomic_write_text
        if target.is_file():
            atomic_write_text(
                pre_backup,
                target.read_text(encoding="utf-8", errors="replace"),
            )
        body = src.read_text(encoding="utf-8", errors="replace")
        atomic_write_text(target, body)
    except OSError as exc:
        return JSONResponse(
            {"ok": False, "error": f"restore failed: {exc}"},
            status_code=500,
        )
    # Bump generation so live sessions see restored version next turn.
    try:
        from xmclaw.daemon.agent_loop import bump_prompt_freeze_generation
        bump_prompt_freeze_generation()
    except Exception:  # noqa: BLE001
        pass
    return JSONResponse({
        "ok": True,
        "restored_from": safe,
        "pre_restore_backup": pre_backup.name,
        "memory_path": str(target),
    })


@router.post("/dream/run")
async def dream_run(request: Request) -> JSONResponse:
    """B-51: on-demand dream pass. Same code path as the daily cron,
    just fires NOW instead of at 03:00 local. Useful for users who want
    to compact MEMORY.md without waiting overnight.

    Returns the compactor's result dict — caller sees backup_path +
    char-count delta on success. Won't run when no LLM configured.
    """
    compactor = getattr(request.app.state, "dream_compactor", None)
    if compactor is None:
        return JSONResponse(
            {"ok": False, "error": "dream not wired (no LLM)"},
            status_code=400,
        )
    result = await compactor.dream()
    return JSONResponse(result, status_code=200 if result.get("ok") else 500)


@router.get("/indexer_status")
async def indexer_status(request: Request) -> JSONResponse:
    """B-49: surface MemoryFileIndexer state for the Memory page.

    Returns ``{wired, running, watched_count, known_count,
    poll_interval_s}``. ``wired=False`` when no embedding provider is
    configured (indexer was never started).
    """
    idx = getattr(request.app.state, "memory_indexer", None)
    if idx is None:
        return JSONResponse({
            "wired": False,
            "reason": "indexer not started (no embedding provider configured)",
        })
    try:
        watched = sum(1 for _ in idx._watched_paths())  # noqa: SLF001
    except Exception:  # noqa: BLE001
        watched = 0
    return JSONResponse({
        "wired": True,
        "running": getattr(idx, "is_running", False),
        "watched_count": watched,
        "known_count": len(getattr(idx, "_known_paths", set()) or set()),
        "poll_interval_s": getattr(idx, "_poll_s", None),
    })


@router.get("/providers")
async def list_providers(request: Request) -> JSONResponse:
    """B-27: enumerate memory providers attached to the running agent.

    Surfaces which providers are wired (always builtin + at most one
    external), each one's name, and whether it has any LLM-callable
    tools registered. Backs the Memory page → Provider panel + the
    upcoming /memory/setup wizard.
    """
    agent = getattr(request.app.state, "agent", None)
    mgr = getattr(agent, "_memory_manager", None) if agent else None
    if mgr is None:
        return JSONResponse({"providers": [], "wired": False})
    out: list[dict[str, Any]] = []
    for p in getattr(mgr, "providers", []):
        try:
            schemas = p.get_tool_schemas() if hasattr(p, "get_tool_schemas") else []
        except Exception:  # noqa: BLE001
            schemas = []
        out.append({
            "name": getattr(p, "name", "?"),
            "kind": "builtin" if getattr(p, "name", "") == "builtin" else "external",
            "tool_count": len(schemas) if isinstance(schemas, list) else 0,
            "tools": [s.get("name") for s in (schemas or []) if isinstance(s, dict)],
        })
    return JSONResponse({
        "providers": out,
        "wired": True,
        "count": len(out),
    })


@router.get("")
async def list_memory() -> JSONResponse:
    """Return filename + size + mtime for every ``*.md`` note.

    Response shape: ``{"files": [...]}``. A missing directory yields
    an empty list — a fresh install is a valid state.
    """
    mdir = file_memory_dir()
    files: list[dict[str, Any]] = []
    if mdir.exists():
        for md in sorted(mdir.glob("*.md")):
            try:
                stat = md.stat()
            except OSError:
                continue
            files.append({
                "name": md.name,
                "path": str(md),
                "size": stat.st_size,
                "mtime": stat.st_mtime,
            })
    return JSONResponse({"files": files})


@router.post("/search")
async def search_memory(request: Request) -> JSONResponse:
    """Substring search across the markdown corpus.

    Intentionally simple: case-insensitive substring, 80-char snippet
    around the first hit per file, score always ``1.0``. FTS5 /
    embedding search is Phase B — the web UI only needs "find a note
    I wrote last week" today, and grep nails that for a corpus measured
    in dozens of files.
    """
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)

    query = str(body.get("query", "")).strip()
    if not query:
        return JSONResponse({"results": []})

    results: list[dict[str, Any]] = []
    mdir = file_memory_dir()
    if mdir.exists():
        needle = query.lower()
        for md in sorted(mdir.glob("*.md")):
            try:
                text = md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            low = text.lower()
            idx = low.find(needle)
            if idx < 0:
                continue
            start = max(0, idx - 80)
            end = min(len(text), idx + len(query) + 80)
            snippet = text[start:end]
            results.append({
                "topic": md.stem,
                "snippet": snippet,
                "score": 1.0,
            })
    return JSONResponse({"results": results})


@router.get("/{filename}")
async def get_memory_file(filename: str) -> JSONResponse:
    """Return one note's full markdown body."""
    mdir = file_memory_dir()
    md = mdir / _safe_name(filename)
    if not md.exists() or not md.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        text = md.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    # B-97: also surface the parsed frontmatter so the UI's edit form
    # can pre-populate the description / tags fields without re-
    # parsing in JS.
    try:
        from xmclaw.providers.memory.file_index import parse_frontmatter
        fields, _body = parse_frontmatter(text)
    except Exception:  # noqa: BLE001
        fields = {}
    desc = fields.get("description") if isinstance(fields, dict) else None
    tags_raw = fields.get("tags") if isinstance(fields, dict) else None
    tags: list[str]
    if isinstance(tags_raw, list):
        tags = [str(t) for t in tags_raw]
    elif isinstance(tags_raw, str) and tags_raw.strip():
        tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
    else:
        tags = []
    return JSONResponse({
        "name": md.name,
        "content": text,
        "description": str(desc) if isinstance(desc, str) else "",
        "tags": tags,
    })


@router.post("/{filename}")
async def save_memory_file(filename: str, request: Request) -> JSONResponse:
    """Upsert a note's body.

    ``.md`` suffix is auto-appended. A missing ``memory/`` dir is
    created — the first save after a clean install must not 500.

    B-97: optional ``description`` (str) and ``tags`` (list[str]) fields
    in the request body are written as YAML frontmatter at the top of
    the saved file — same shape that ``note_write`` agent tool produces
    (B-93). Body content is rendered AFTER the frontmatter, so users
    can edit just the body in the UI without their tags getting wiped.
    """
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)

    mdir = file_memory_dir()
    mdir.mkdir(parents=True, exist_ok=True)
    name = _safe_name(filename)
    md = mdir / name
    content = str(body.get("content", ""))
    description = str(body.get("description", "") or "").strip()
    tags_raw = body.get("tags") or []
    tags: list[str] = (
        [str(t).strip() for t in tags_raw if str(t).strip()]
        if isinstance(tags_raw, list) else []
    )

    # B-97: build frontmatter when either field is non-empty. Strip
    # any pre-existing frontmatter from ``content`` first so we don't
    # double-stack on edits.
    final_body = content
    if description or tags:
        try:
            from xmclaw.providers.memory.file_index import parse_frontmatter
            _existing_fields, stripped_body = parse_frontmatter(content)
            final_body = stripped_body if stripped_body else content
        except Exception:  # noqa: BLE001
            pass
        lines = ["---"]
        if description:
            # ``---`` inside the value would terminate the block early —
            # rewrite to em-dash. Same defence as note_write tool.
            lines.append(f"description: {description.replace('---', '—')}")
        if tags:
            lines.append("tags: [" + ", ".join(tags) + "]")
        lines.append("---")
        lines.append("")
        frontmatter = "\n".join(lines) + "\n"
        final_body = frontmatter + final_body.lstrip()

    # B-74: atomic write so a daemon crash mid-save can't truncate the
    # user's note. The note_write agent tool already used this pattern
    # (B-71); the UI's POST path was the missing twin.
    from xmclaw.utils.fs_locks import atomic_write_text
    atomic_write_text(md, final_body)
    return JSONResponse({"ok": True, "name": name})
