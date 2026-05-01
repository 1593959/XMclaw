"""Channels API — manifest discovery + per-channel config CRUD.

B-147. Mounted at ``/api/v2/channels``. Backs the Channels page so
users configure 飞书 / 钉钉 / 企微 etc. from the Web UI without
hand-editing ``daemon/config.json``.

GET ``/api/v2/channels`` — list every known manifest plus the current
config snapshot per channel + runtime status from the dispatcher.
Response shape::

    {
      "channels": [
        {
          "id": "feishu",
          "label": "飞书 / Lark",
          "implementation_status": "ready",
          "needs_tunnel": false,
          "requires": ["lark-oapi>=1.4.0"],
          "config_schema": {"app_id": "string (required)", ...},
          "config": {"enabled": false, "app_id": "", ...},
          "running": false
        },
        ...
      ]
    }

PUT ``/api/v2/channels/{id}`` — upsert one channel's config. Body is
the channel's full config dict (``{"enabled": true, "app_id": "..."}``).
Empty / missing secret-shaped fields preserve the on-disk value
(same as ``PUT /api/v2/config/llm``). ``restart_required: true`` is
always returned because adapters bind credentials at start time.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse


router = APIRouter(prefix="/api/v2/channels", tags=["channels"])


# Field names whose value is a credential — same redaction posture as
# the rest of the config endpoints. Empty strings on PUT preserve the
# on-disk value rather than clearing it.
_SECRET_KEYS = frozenset({
    "app_secret", "client_secret", "corp_secret", "encrypt_key",
    "encoding_aes_key", "verify_token", "verification_token",
    "bot_token", "token", "secret",
})


def _redact(v: Any) -> Any:
    if not isinstance(v, str) or not v:
        return v
    if len(v) <= 8:
        return "***"
    return f"{v[:4]}…{v[-4:]}"


def _redact_channel_cfg(cfg: dict) -> dict:
    """Mask secret fields for GET — keeps non-secret fields visible
    so the UI can show app_id / chat_id without leaking credentials."""
    out: dict = {}
    for k, v in cfg.items():
        if k in _SECRET_KEYS:
            out[k] = _redact(v)
        else:
            out[k] = v
    return out


def _config_path(request: Request) -> Path | None:
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
async def list_channels(request: Request) -> JSONResponse:
    """Return every channel manifest + current config + runtime status."""
    from xmclaw.providers.channel.registry import discover

    manifests = discover(include_scaffolds=True)

    cfg = getattr(request.app.state, "config", None) or {}
    channels_section = (
        cfg.get("channels") if isinstance(cfg.get("channels"), dict) else {}
    )

    dispatcher = getattr(request.app.state, "channel_dispatcher", None)
    running_ids: set[str] = set()
    if dispatcher is not None:
        for a in getattr(dispatcher, "_adapters", []):
            running_ids.add(getattr(a, "name", ""))

    out: list[dict[str, Any]] = []
    for cid, m in manifests.items():
        ch_cfg = channels_section.get(cid) or {}
        if not isinstance(ch_cfg, dict):
            ch_cfg = {}
        out.append({
            "id": m.id,
            "label": m.label,
            "implementation_status": m.implementation_status,
            "needs_tunnel": m.needs_tunnel,
            "requires": list(m.requires),
            "config_schema": dict(m.config_schema),
            "config": _redact_channel_cfg(ch_cfg),
            "running": cid in running_ids,
        })
    return JSONResponse({"channels": out})


@router.put("/{channel_id}")
async def upsert_channel(
    channel_id: str, request: Request, payload: dict[str, Any],
) -> JSONResponse:
    """Write one channel's config to ``config.channels.<id>``.

    Empty-string secret fields preserve the on-disk value (UI can
    leave api_key blank to edit only ``enabled`` / non-secret keys).
    """
    from xmclaw.providers.channel.registry import discover

    manifests = discover(include_scaffolds=True)
    if channel_id not in manifests:
        return JSONResponse(
            {"ok": False, "error": f"unknown channel {channel_id!r}"},
            status_code=400,
        )
    if not isinstance(payload, dict):
        return JSONResponse(
            {"ok": False, "error": "body must be a JSON object"},
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

    channels = cfg.setdefault("channels", {})
    if not isinstance(channels, dict):
        channels = {}
        cfg["channels"] = channels
    existing = channels.get(channel_id) or {}
    if not isinstance(existing, dict):
        existing = {}

    merged: dict[str, Any] = dict(existing)
    for k, v in payload.items():
        if k in _SECRET_KEYS:
            # Empty / redacted-looking → keep the on-disk value.
            if not isinstance(v, str) or not v.strip():
                continue
            # Reject masked values like 'abcd…wxyz' so the UI can't
            # accidentally write the redacted form back.
            if "…" in v:
                continue
        merged[k] = v
    channels[channel_id] = merged

    try:
        _atomic_write(target, cfg)
    except OSError as exc:
        return JSONResponse({"ok": False, "error": f"write failed: {exc}"}, status_code=500)

    # Update the in-memory config so subsequent requests see fresh
    # values without a daemon restart (adapter rebind still requires
    # restart — surfaced via restart_required flag).
    in_mem = getattr(request.app.state, "config", None)
    if isinstance(in_mem, dict):
        in_mem.setdefault("channels", {})[channel_id] = merged

    return JSONResponse({
        "ok": True,
        "id": channel_id,
        "path": str(target),
        "restart_required": True,
    })
