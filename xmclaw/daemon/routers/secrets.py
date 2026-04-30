"""B-104: secrets router — Web UI parity with the ``xmclaw config
{set,get,list,delete}-secret`` CLI commands.

Lets users manage entries in the secrets store from the browser
instead of dropping into a terminal. The store itself
(``xmclaw/utils/secrets.py``) was Epic #16 Phase 1 — full-featured
including the Fernet-at-rest encrypted backend — but the daemon never
exposed an HTTP surface for it, so the only way in was the CLI.

Endpoints:
  GET    /api/v2/secrets                  — list names + env override flags
  POST   /api/v2/secrets                  — set or update a secret
  DELETE /api/v2/secrets/{name}           — delete a secret

The secret VALUES are never returned over the wire (read happens at
daemon-config-resolve time, not via this API). The UI only sees the
name + whether an env var is currently shadowing it.
"""
from __future__ import annotations


from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

from xmclaw.utils.secrets import (
    delete_secret,
    is_encryption_available,
    iter_env_override_names,
    list_secret_names,
    set_secret,
)

router = APIRouter(prefix="/api/v2/secrets", tags=["secrets"])


@router.get("")
async def list_all() -> JSONResponse:
    """Return secret names from the file store + which ones are
    overridden by env vars (so the user knows their saved value isn't
    the one in use). Values themselves are NEVER returned."""
    try:
        names = list_secret_names()
    except Exception:  # noqa: BLE001
        names = []
    try:
        overrides = set(iter_env_override_names())
    except Exception:  # noqa: BLE001
        overrides = set()
    items = [
        {"name": n, "env_override": n in overrides}
        for n in names
    ]
    # Also surface env-only overrides — names that exist as
    # XMC_SECRET_<NAME> env vars but NOT in the file store. The CLI
    # ``list-secrets`` flags these the same way.
    file_set = set(names)
    for ov in overrides:
        if ov not in file_set:
            items.append({"name": ov, "env_override": True, "env_only": True})
    items.sort(key=lambda x: x["name"])
    return JSONResponse({
        "items": items,
        "encryption_available": is_encryption_available(),
    })


@router.post("")
async def set(request: Request) -> JSONResponse:
    """Body: ``{name, value, backend?}``. Default backend = "encrypted"
    when cryptography is available, falls back to "file" otherwise.
    Returns the resolved backend so the UI can show "stored encrypted"
    vs "stored plaintext" feedback.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    name = str(body.get("name", "") or "").strip()
    value = body.get("value")
    if not name:
        return JSONResponse({"ok": False, "error": "name is required"}, status_code=400)
    if not isinstance(value, str):
        return JSONResponse({"ok": False, "error": "value must be a string"}, status_code=400)
    requested = str(body.get("backend", "")).strip().lower()
    if requested not in ("encrypted", "file", "keyring", ""):
        return JSONResponse(
            {"ok": False, "error": f"unknown backend {requested!r}"},
            status_code=400,
        )
    backend = requested or ("encrypted" if is_encryption_available() else "file")
    try:
        set_secret(name, value, backend=backend)  # type: ignore[arg-type]
    except RuntimeError as exc:
        # Missing optional dep (cryptography / keyring) — fall through
        # cleanly so the UI shows the real reason.
        return JSONResponse(
            {"ok": False, "error": str(exc)}, status_code=400,
        )
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
            status_code=500,
        )
    return JSONResponse({"ok": True, "name": name, "backend": backend})


@router.delete("/{name}")
async def delete(name: str) -> JSONResponse:
    try:
        removed = delete_secret(name)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            {"ok": False, "error": str(exc)}, status_code=500,
        )
    return JSONResponse({"ok": bool(removed), "removed": bool(removed)})
