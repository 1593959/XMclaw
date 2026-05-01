"""LLM Profiles API — list / upsert / delete deployed model profiles.

Mounted at ``/api/v2/llm/profiles``. Backs the Settings page so the
user can manage multiple LLM endpoints (Anthropic + OpenAI + a local
DeepSeek over an OpenAI-compatible base URL, …) and pick which one
to route each chat session through.

GET returns the list with ``api_key`` redacted. POST upserts a profile
into ``daemon/config.json`` and returns ``restart_required: true`` —
the in-memory ``LLMRegistry`` is built at boot and we don't hot-swap
SDK clients. DELETE removes a profile by id (also restart-required).

The default profile (``id == "default"``) is synthesised by the
factory from the legacy ``llm.{anthropic,openai}`` block; it's
visible in GET but cannot be POSTed/DELETEd from this surface — the
existing ``PUT /api/v2/config/llm`` endpoint owns that block.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

router = APIRouter(prefix="/api/v2/llm/profiles", tags=["llm-profiles"])


_VALID_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_VALID_PROVIDERS = ("anthropic", "openai")


def _redact_key(key: str | None) -> str:
    """Show only the suffix so the user can recognise which key is set
    without leaking the secret. Empty / unset → ``""`` so the UI can
    show 'not configured' rather than a misleading mask."""
    if not key:
        return ""
    if len(key) <= 8:
        return "***"
    return f"{key[:4]}…{key[-4:]}"


def _config_path(request: Request) -> Path | None:
    """Where to write the updated config. Same fallback logic as
    ``PUT /api/v2/config/llm`` so the two endpoints stay coherent."""
    cfg_path = getattr(request.app.state, "config_path", None)
    if cfg_path:
        return Path(cfg_path)
    fallback = Path("daemon") / "config.json"
    return fallback if fallback.exists() else None


def _load_config(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise ValueError(f"existing config is invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        return {}
    return data


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)


@router.get("")
async def list_profiles(request: Request) -> JSONResponse:
    """Return all profiles known to the running registry, plus the
    raw on-disk profiles list (so the UI can show entries the daemon
    couldn't load — e.g. missing api_key — in a 'broken' state)."""
    registry = getattr(request.app.state, "llm_registry", None)
    runtime: list[dict[str, Any]] = []
    default_id: str | None = None
    if registry is not None:
        default_id = registry.default_id
        for prof in registry:
            runtime.append({
                "id": prof.id,
                "label": prof.label,
                "provider": prof.provider_name,
                "model": prof.model,
                "is_default": prof.id == registry.default_id,
            })

    on_disk: list[dict[str, Any]] = []
    cfg = getattr(request.app.state, "config", None)
    if isinstance(cfg, dict):
        llm = cfg.get("llm")
        if isinstance(llm, dict):
            raw = llm.get("profiles")
            if isinstance(raw, list):
                for entry in raw:
                    if not isinstance(entry, dict):
                        continue
                    on_disk.append({
                        "id": str(entry.get("id") or ""),
                        "label": str(entry.get("label") or ""),
                        "provider": str(entry.get("provider") or ""),
                        "model": str(entry.get("model") or entry.get("default_model") or ""),
                        "base_url": str(entry.get("base_url") or ""),
                        "api_key_redacted": _redact_key(
                            entry.get("api_key") if isinstance(entry.get("api_key"), str) else "",
                        ),
                    })

    return JSONResponse({
        "profiles": runtime,
        "on_disk": on_disk,
        "default_id": default_id,
    })


@router.post("")
async def upsert_profile(request: Request, payload: dict[str, Any]) -> JSONResponse:
    """Add or replace one named profile in ``daemon/config.json``.

    Body schema::

        {
          "id": "haiku-fast",          required, slug
          "label": "Claude Haiku",     optional
          "provider": "anthropic",     required, "anthropic" | "openai"
          "model": "claude-haiku-4-5", required
          "api_key": "sk-...",         required
          "base_url": "https://..."    optional
        }

    The reserved id ``"default"`` is rejected — that block is owned by
    ``PUT /api/v2/config/llm``. ``restart_required`` is always true on
    success because the in-memory registry doesn't hot-swap.
    """
    pid = str(payload.get("id") or "").strip()
    if not pid or not _VALID_ID.match(pid):
        return JSONResponse(
            {"ok": False, "error": "id must match [a-z0-9][a-z0-9_-]{0,63}"},
            status_code=400,
        )
    if pid == "default":
        return JSONResponse(
            {"ok": False, "error": "id 'default' is reserved — use PUT /api/v2/config/llm"},
            status_code=400,
        )

    provider = str(payload.get("provider") or "").strip().lower()
    if provider not in _VALID_PROVIDERS:
        return JSONResponse(
            {"ok": False, "error": f"provider must be one of {list(_VALID_PROVIDERS)}"},
            status_code=400,
        )

    model = str(payload.get("model") or "").strip()
    api_key = str(payload.get("api_key") or "").strip()
    if not model:
        return JSONResponse({"ok": False, "error": "model is required"}, status_code=400)
    base_url = str(payload.get("base_url") or "").strip()
    label = str(payload.get("label") or "").strip() or pid

    target = _config_path(request)
    if target is None:
        return JSONResponse(
            {"ok": False, "error": "daemon has no config_path; cannot persist"},
            status_code=500,
        )
    try:
        cfg = _load_config(target)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    llm = cfg.setdefault("llm", {})
    if not isinstance(llm, dict):
        llm = {}
        cfg["llm"] = llm
    profiles = llm.setdefault("profiles", [])
    if not isinstance(profiles, list):
        profiles = []
        llm["profiles"] = profiles

    new_entry: dict[str, Any] = {
        "id": pid,
        "label": label,
        "provider": provider,
        "model": model,
    }
    if base_url:
        new_entry["base_url"] = base_url
    # Preserve existing api_key when caller submits an empty string (UI
    # convention: empty means "leave the secret alone, edit only label/
    # model/base_url"). Same pattern as PUT /api/v2/config/llm.
    existing = next((e for e in profiles if isinstance(e, dict) and e.get("id") == pid), None)
    if api_key:
        new_entry["api_key"] = api_key
    elif isinstance(existing, dict) and isinstance(existing.get("api_key"), str):
        new_entry["api_key"] = existing["api_key"]
    else:
        # B-146: skip the require-api_key check when the legacy
        # same-provider block has a key the profile can inherit.
        # Build-time falls through to llm.<provider>.api_key in
        # build_llm_profiles_from_config; persisting an empty string
        # here just means "use whatever the legacy key resolves to".
        legacy_pcfg = llm.get(provider)
        legacy_key = (
            legacy_pcfg.get("api_key")
            if isinstance(legacy_pcfg, dict)
            else None
        )
        if isinstance(legacy_key, str) and legacy_key.strip():
            # Don't write the secret into the new entry — just let it
            # inherit. Keeps the on-disk config DRY.
            pass
        else:
            return JSONResponse(
                {
                    "ok": False,
                    "error": (
                        f"api_key required: no legacy llm.{provider}.api_key "
                        "to inherit from. Set the provider's key in 设置 "
                        "first, then this profile can leave api_key blank."
                    ),
                },
                status_code=400,
            )

    if existing is None:
        profiles.append(new_entry)
    else:
        idx = profiles.index(existing)
        profiles[idx] = new_entry

    try:
        _atomic_write(target, cfg)
    except OSError as exc:
        return JSONResponse({"ok": False, "error": f"write failed: {exc}"}, status_code=500)

    return JSONResponse({
        "ok": True,
        "id": pid,
        "path": str(target),
        "restart_required": True,
    })


@router.delete("/{profile_id}")
async def delete_profile(request: Request, profile_id: str) -> JSONResponse:
    """Remove one profile from config.json. Idempotent (deleting an
    unknown id returns ok=True)."""
    if profile_id == "default":
        return JSONResponse(
            {"ok": False, "error": "cannot delete the default profile"},
            status_code=400,
        )
    target = _config_path(request)
    if target is None:
        return JSONResponse(
            {"ok": False, "error": "daemon has no config_path; cannot persist"},
            status_code=500,
        )
    try:
        cfg = _load_config(target)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    llm = cfg.get("llm")
    if isinstance(llm, dict):
        profiles = llm.get("profiles")
        if isinstance(profiles, list):
            kept = [e for e in profiles if not (isinstance(e, dict) and e.get("id") == profile_id)]
            if len(kept) != len(profiles):
                llm["profiles"] = kept
                try:
                    _atomic_write(target, cfg)
                except OSError as exc:
                    return JSONResponse(
                        {"ok": False, "error": f"write failed: {exc}"},
                        status_code=500,
                    )

    return JSONResponse({"ok": True, "id": profile_id, "restart_required": True})


@router.put("/default")
async def set_default_profile(request: Request, payload: dict[str, Any]) -> JSONResponse:
    """B-146: pick which profile is the daemon-wide default.

    Writes ``llm.default_profile_id = "<id>"`` to ``config.json``.
    The factory's ``build_llm_registry_from_config`` honors that on
    next boot. Empty string clears the override and falls back to the
    legacy ``"default"`` profile.
    """
    new_id = str(payload.get("id") or "").strip()
    target = _config_path(request)
    if target is None:
        return JSONResponse(
            {"ok": False, "error": "daemon has no config_path; cannot persist"},
            status_code=500,
        )
    try:
        cfg = _load_config(target)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    llm = cfg.setdefault("llm", {})
    if not isinstance(llm, dict):
        llm = {}
        cfg["llm"] = llm
    # Validate the requested id exists somewhere — runtime registry OR
    # on-disk profiles list OR the synthesised "default".
    valid_ids: set[str] = {"default", ""}
    profiles = llm.get("profiles") if isinstance(llm.get("profiles"), list) else []
    for entry in profiles:
        if isinstance(entry, dict) and entry.get("id"):
            valid_ids.add(str(entry["id"]))
    if new_id and new_id not in valid_ids:
        return JSONResponse(
            {"ok": False, "error": f"unknown profile id {new_id!r}"},
            status_code=400,
        )
    if new_id:
        llm["default_profile_id"] = new_id
    else:
        llm.pop("default_profile_id", None)
    try:
        _atomic_write(target, cfg)
    except OSError as exc:
        return JSONResponse({"ok": False, "error": f"write failed: {exc}"}, status_code=500)
    return JSONResponse({
        "ok": True,
        "default_profile_id": new_id,
        "restart_required": True,
    })
