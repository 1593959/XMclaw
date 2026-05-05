"""Profiles API — list + read + edit persona markdown files.

Mounted at ``/api/v2/profiles`` by :func:`xmclaw.daemon.app.create_app`.
Backs the web-UI "身份与记忆" panel (Memory page → 标识 tab) and the
older read-only persona picker.

Three surfaces:

* ``GET /api/v2/profiles`` — list every ``*.md`` under
  :func:`xmclaw.utils.paths.persona_dir` (legacy flat layout).
* ``GET /api/v2/profiles/active`` — 7 canonical persona files
  (SOUL / AGENTS / USER / MEMORY / IDENTITY / TOOLS / BOOTSTRAP) of the
  *active* profile, with per-file ``layer`` ("project" / "profile" /
  "builtin") so the UI can show where each one came from.
* ``PUT /api/v2/profiles/active/{file_id}`` — upsert one of the
  canonical files in the active profile directory, then nudge the
  running ``AgentLoop`` to rebuild its system prompt so edits land on
  the very next turn (no daemon restart needed).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

from xmclaw.utils.paths import persona_dir

router = APIRouter(prefix="/api/v2/profiles", tags=["profiles"])


# ──────────────────────────────────────────────────────────────────────
# Active profile resolution + canonical file list
# ──────────────────────────────────────────────────────────────────────


_ALLOWED_BASENAMES: tuple[str, ...] = (
    "AGENTS.md",
    "SOUL.md",
    "IDENTITY.md",
    "USER.md",
    "TOOLS.md",
    "BOOTSTRAP.md",
    "MEMORY.md",
)


def _basename_lookup(file_id: str) -> str | None:
    """Resolve a UI-supplied id (case-insensitive, with-or-without .md)
    to one of the canonical persona basenames. Returns None on miss."""
    raw = file_id.replace("/", "_").replace("\\", "_").strip()
    if not raw:
        return None
    needle = raw.lower()
    if not needle.endswith(".md"):
        needle = f"{needle}.md"
    for canonical in _ALLOWED_BASENAMES:
        if canonical.lower() == needle:
            return canonical
    return None


def _resolve_active_profile_dir(request: Request) -> tuple[str, Path]:
    """Mirror :func:`xmclaw.daemon.factory._resolve_persona_profile_dir`.

    Reads ``app.state.config["persona"]["profile_id"]`` (falls back to
    ``"default"``). Inline-text profiles (rare) point at the special
    ``_inline`` directory. Inlined here instead of imported so the
    profiles router never pulls in the whole factory module on startup.
    """
    cfg: Any = getattr(request.app.state, "config", None) or {}
    persona = cfg.get("persona") if isinstance(cfg, dict) else None
    pdir_root = persona_dir().parent / "profiles"
    if isinstance(persona, dict):
        pid = persona.get("profile_id")
        if isinstance(pid, str) and pid.strip():
            stem = pid.strip().replace("/", "_").replace("\\", "_")
            return stem, pdir_root / stem
        inline = persona.get("text")
        if isinstance(inline, str) and inline.strip():
            return "_inline", pdir_root / "_inline"
    return "default", pdir_root / "default"


# ──────────────────────────────────────────────────────────────────────
# Legacy flat listing (kept for the older persona-picker UI)
# ──────────────────────────────────────────────────────────────────────


@router.get("")
async def list_profiles() -> JSONResponse:
    """Return every ``*.md`` directly under :func:`persona_dir`."""
    pdir = persona_dir()
    profiles: list[dict[str, Any]] = []
    if pdir.exists():
        for md in sorted(pdir.glob("*.md")):
            try:
                text = md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            title = md.stem
            for line in text.splitlines():
                line = line.strip()
                if line:
                    title = line.lstrip("# ").strip() or md.stem
                    break
            profiles.append({"id": md.stem, "title": title, "path": str(md)})
    return JSONResponse({"profiles": profiles})


# ──────────────────────────────────────────────────────────────────────
# Active-profile editing surface
# ──────────────────────────────────────────────────────────────────────


@router.post("/active/dedupe")
async def dedupe_active_profile(request: Request) -> JSONResponse:
    """One-shot cleanup of duplicate bullets accumulated by earlier
    dedup-less reflection runs. Walks each of the 7 canonical persona
    files in the active profile dir, collapses semantically-identical
    bullets (keeping first occurrence), reports what was dropped.

    Idempotent — running twice in a row makes no further changes.
    """
    from xmclaw.providers.tool.builtin import collapse_existing_duplicates

    profile_id, pdir = _resolve_active_profile_dir(request)
    pdir.mkdir(parents=True, exist_ok=True)
    summary: list[dict[str, Any]] = []
    for canonical in _ALLOWED_BASENAMES:
        path = pdir / canonical
        if not path.is_file():
            continue
        try:
            before = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        after = collapse_existing_duplicates(before)
        if after == before:
            summary.append({
                "file": canonical,
                "before_bytes": len(before.encode("utf-8")),
                "after_bytes": len(before.encode("utf-8")),
                "removed_lines": 0,
            })
            continue
        try:
            # B-74: atomic write so a crash mid-dedup can't truncate
            # the persona file the agent's identity depends on.
            from xmclaw.utils.fs_locks import atomic_write_text
            atomic_write_text(path, after)
        except OSError as exc:
            summary.append({"file": canonical, "error": str(exc)})
            continue
        removed = before.count("\n") - after.count("\n")
        summary.append({
            "file": canonical,
            "before_bytes": len(before.encode("utf-8")),
            "after_bytes": len(after.encode("utf-8")),
            "removed_lines": removed,
        })

    # Bust the system-prompt cache so the next agent turn reads the
    # cleaned-up files.
    try:
        from xmclaw.core.persona.assembler import clear_cache
        clear_cache()
    except Exception:  # noqa: BLE001
        pass

    return JSONResponse({
        "ok": True,
        "profile_id": profile_id,
        "files": summary,
    })


@router.get("/active/agent_writes")
async def list_agent_writes(request: Request) -> JSONResponse:
    """Return the agent-wrote-this sidecar log so the Memory UI can
    show diff badges. Each row is a write event recorded by the
    persona-modifying tools (remember / learn_about_user / update_persona).
    """
    profile_id, pdir = _resolve_active_profile_dir(request)
    sidecar = pdir / ".agent_writes.jsonl"
    if not sidecar.is_file():
        return JSONResponse({"writes": [], "profile_id": profile_id})
    rows: list[dict[str, Any]] = []
    try:
        for line in sidecar.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return JSONResponse({
        "writes": rows[-200:],  # cap response payload
        "profile_id": profile_id,
    })


@router.get("/active")
async def get_active_profile(request: Request) -> JSONResponse:
    """Return the 7 canonical persona files of the active profile.

    Each entry carries:
      * ``basename`` — canonical-cased filename (e.g. ``"SOUL.md"``)
      * ``content`` — current text. Falls back to the bundled template
        when the user hasn't materialized a profile copy yet.
      * ``layer`` — ``"project"`` / ``"profile"`` / ``"builtin"`` so the
        UI can show "this is a built-in default" vs "you've edited this"
      * ``source`` — absolute path (or ``<builtin:...>`` sentinel)
      * ``exists`` — whether the active profile dir actually has this file
        on disk (so the UI can offer a "Save (creates file)" CTA).
    """
    profile_id, pdir = _resolve_active_profile_dir(request)
    # Load with builtin fallback so the UI always has SOMETHING to show
    # — first install means profile dir is empty.
    from xmclaw.core.persona.loader import load_persona_files

    files = load_persona_files(
        profile_dir=pdir,
        workspace_dir=None,
        include_builtin_fallback=True,
    )
    by_basename: dict[str, dict[str, Any]] = {}
    for f in files:
        # Return *raw* file content (incl. YAML frontmatter) when the
        # source is a real file. ``load_persona_files`` strips
        # frontmatter for prompt-assembly purposes; if we returned
        # the stripped form, a round-trip GET → edit → PUT would
        # silently delete the frontmatter. For builtin-template files
        # there is no on-disk path yet, so we hand back the loader's
        # already-stripped content.
        content = f.content
        if f.layer != "builtin":
            try:
                content = Path(f.source).read_text(
                    encoding="utf-8", errors="replace",
                )
            except OSError:
                content = f.content
        by_basename[f.basename] = {
            "basename": f.basename,
            "content": content,
            "layer": f.layer,
            "source": str(f.source),
            "exists": f.layer != "builtin"
                and Path(f.source).is_file(),
            "order": f.order,
        }
    # Even files we couldn't resolve (e.g. BOOTSTRAP.md absent + opt-in
    # template not bundled) get a stub so the UI can offer "create" UX.
    for canonical in _ALLOWED_BASENAMES:
        by_basename.setdefault(canonical, {
            "basename": canonical,
            "content": "",
            "layer": "missing",
            "source": str(pdir / canonical),
            "exists": False,
            "order": 999,
        })
    ordered = sorted(by_basename.values(), key=lambda d: d["order"])
    return JSONResponse({
        "profile_id": profile_id,
        "profile_dir": str(pdir),
        "files": ordered,
    })


@router.put("/active/{file_id}")
async def upsert_active_profile_file(
    file_id: str, request: Request,
) -> JSONResponse:
    """Write content to one of the 7 canonical files in the active profile.

    Body: ``{"content": "..."}``. Creates the profile directory if it
    doesn't exist yet — matches the UX where a fresh install can save
    a custom SOUL.md without first running ``ensure_default_profile``.

    Side effect: rebuilds the system prompt on ``app.state.agent`` so
    the next turn picks up the edit. Failure to rebuild is logged but
    does not roll back the write — users can always restart the daemon.
    """
    canonical = _basename_lookup(file_id)
    if canonical is None:
        return JSONResponse(
            {"ok": False, "error": f"unknown file_id {file_id!r}; expected one of "
             + ", ".join(_ALLOWED_BASENAMES)},
            status_code=400,
        )
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse(
            {"ok": False, "error": "invalid json"}, status_code=400,
        )
    if not isinstance(body, dict):
        return JSONResponse(
            {"ok": False, "error": "invalid json"}, status_code=400,
        )
    content = str(body.get("content", ""))

    profile_id, pdir = _resolve_active_profile_dir(request)
    pdir.mkdir(parents=True, exist_ok=True)
    target = pdir / canonical

    # B-198 Phase 3: route through PersonaStore when wired so the Web
    # UI edit lands in the DB (truth) and the disk file becomes the
    # render of that. Auto-extracted bullets the user round-tripped
    # are stripped by set_manual — they're derived from fact rows,
    # not user-editable. Falls back to legacy direct-disk write when
    # the store isn't configured (tests / daemons without vec store).
    store = getattr(request.app.state, "persona_store", None)
    if store is not None:
        try:
            await store.set_manual(canonical, content)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                {"ok": False, "error": f"store write failed: {exc}"},
                status_code=500,
            )
    else:
        # B-74: atomic write — agent identity files get rewritten via
        # this endpoint when the user edits them in the Web UI Memory
        # page. A daemon crash mid-save would otherwise corrupt the
        # file the agent's persona depends on. update_persona /
        # remember tool paths already use this pattern (B-71); the
        # UI's POST path was the missing twin.
        from xmclaw.utils.fs_locks import atomic_write_text
        atomic_write_text(target, content)

    # Best-effort: bust the assembled-prompt cache + nudge the running
    # AgentLoop to rebuild on the next turn. The assembler cache is
    # mtime-keyed so just clearing is technically redundant — but we
    # also want app.state.agent._system_prompt to refresh, otherwise
    # the long-lived loop keeps the stale cached string.
    try:
        from xmclaw.core.persona import build_system_prompt
        from xmclaw.core.persona.assembler import clear_cache
        clear_cache()
        agent = getattr(request.app.state, "agent", None)
        if agent is not None:
            tool_specs = []
            try:
                tools = getattr(agent, "_tools", None)
                if tools is not None:
                    tool_specs = tools.list_tools() or []
            except Exception:  # noqa: BLE001
                tool_specs = []
            ws_root = None
            try:
                from xmclaw.core.workspace import WorkspaceManager
                ws = WorkspaceManager().get()
                if ws.primary is not None:
                    ws_root = Path(ws.primary.path)
            except Exception:  # noqa: BLE001
                ws_root = None
            new_prompt = build_system_prompt(
                profile_dir=pdir,
                workspace_dir=ws_root,
                tool_names=[s.name for s in tool_specs],
            )
            agent._system_prompt = new_prompt  # noqa: SLF001
    except Exception as exc:  # noqa: BLE001
        from xmclaw.utils.log import get_logger
        get_logger(__name__).warning(
            "profiles.system_prompt_rebuild_failed",
            extra={"err": str(exc), "file": canonical},
        )

    return JSONResponse({
        "ok": True,
        "profile_id": profile_id,
        "basename": canonical,
        "path": str(target),
        "size": len(content.encode("utf-8")),
    })


@router.get("/{profile_id}")
async def get_profile(profile_id: str) -> JSONResponse:
    """Return the full markdown content for one legacy flat profile."""
    pdir = persona_dir()
    safe_id = profile_id.replace("/", "_").replace("\\", "_")
    md = pdir / f"{safe_id}.md"
    if not md.exists() or not md.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        text = md.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse({"id": safe_id, "content": text})
