"""LLM Endpoint Discovery — discover models from OpenAI-compatible endpoints.

Mounted at ``/api/v2/llm/endpoints``. Backs the "Discover Models" section
of the Settings page: the user enters a base_url + api_key, clicks
"Fetch", and the endpoint hits ``GET /v1/models`` to retrieve the full
list of available models (id, name, created, context window if exposed).

Endpoints
---------
* ``POST /api/v2/llm/endpoints/discover`` — hit ``GET /v1/models`` and
  return the raw model list grouped by endpoint (url+key pair).
* ``POST /api/v2/llm/endpoints/apply`` — bulk-create profiles from
  discovered models. The user selects which models to add; the endpoint
  writes them into ``daemon/config.json`` under a single endpoint group.

Design:
  * Discovery is stateless — no caching, no server-side state. The
    frontend is responsible for displaying discovered models while the
    user makes selections.
  * ``apply`` creates one profile per selected model. All profiles share
    the same provider/base_url/api_key, keyed by an ``endpoint_id``
    (sha256(url+key) truncated to 12 hex chars).
  * The ``endpoint_id`` lets the UI group profiles by source and re-use
    the key on the next discover call.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

router = APIRouter(prefix="/api/v2/llm/endpoints", tags=["llm-discovery"])

_VALID_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _endpoint_id(base_url: str, api_key: str) -> str:
    """Deterministic id for a url+key pair — used to group profiles."""
    raw = f"{base_url.strip()}::{api_key.strip()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def _redact_key(key: str | None) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "***"
    return f"{key[:4]}...{key[-4:]}"


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


@router.post("/discover")
async def discover_models(request: Request) -> JSONResponse:
    """Fetch the model list from an OpenAI-compatible endpoint.

    Body::

        {
          "base_url": "https://api.openai.com/v1",
          "api_key": "sk-...",
          "provider": "openai" | "anthropic" | "openrouter" | "openai_compat"
        }

    Returns the raw model list from ``GET /v1/models`` with metadata:
    count, elapsed_ms, and whether the endpoint appeared reachable.

    For Anthropic-shaped endpoints (which don't expose /v1/models), we
    attempt a smoke test (1-token completion) to validate connectivity.
    """
    payload = await request.json()
    base_url = str(payload.get("base_url") or "").strip()
    api_key = str(payload.get("api_key") or "").strip()
    provider = str(payload.get("provider") or "openai_compat").strip().lower()

    if not base_url:
        return JSONResponse(
            {"ok": False, "error": "base_url is required"}, status_code=400
        )
    if not api_key:
        return JSONResponse(
            {"ok": False, "error": "api_key is required"}, status_code=400
        )

    import httpx

    t0 = time.perf_counter()
    headers: dict[str, str] = {}
    if provider in ("openai", "openrouter", "openai_compat"):
        headers["Authorization"] = f"Bearer {api_key}"
    elif provider == "anthropic":
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"

    # Normalize base_url: strip trailing slash, avoid double /v1
    base_url = base_url.rstrip('/')
    if not base_url.endswith('/v1'):
        base_url = f'{base_url}/v1'
    models_url = f'{base_url}/models'
    result: dict[str, Any] = {
        "ok": True,
        "base_url": base_url,
        "endpoint_id": _endpoint_id(base_url, api_key),
        "provider": provider,
        "api_key_redacted": _redact_key(api_key),
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(models_url, headers=headers)

        if resp.status_code == 200:
            data = resp.json()
            items = data.get("data", [])
            if not isinstance(items, list):
                items = []
            parsed = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                mid = item.get("id", "")
                mname = item.get("name", mid)
                # Skip if no id
                if not mid or not isinstance(mid, str):
                    continue
                entry: dict[str, Any] = {
                    "id": mid,
                    "name": mname if isinstance(mname, str) else mid,
                }
                # Extract context length if available
                meta = item.get("capabilities", {})
                if isinstance(meta, dict):
                    ctx = meta.get("context_length")
                    if isinstance(ctx, int) and ctx > 0:
                        entry["context_length"] = ctx
                # 2026-06-15: vision detection so the UI can pre-light the
                # 👁 toggle. Standard /v1/models gives NO modality info, so
                # for most endpoints this falls back to the name heuristic.
                # OpenRouter is the exception — it returns ``architecture``
                # with real input modalities, which we read directly.
                vis: bool | None = None
                arch = item.get("architecture")
                if isinstance(arch, dict):
                    ims = arch.get("input_modalities")
                    if isinstance(ims, list):
                        vis = any(str(x).lower() == "image" for x in ims)
                    elif isinstance(arch.get("modality"), str):
                        vis = "image" in arch["modality"].lower()
                if vis is None:
                    try:
                        from xmclaw.providers.llm.openai import _model_supports_vision
                        vis = _model_supports_vision(mid, base_url)
                    except Exception:  # noqa: BLE001
                        vis = False
                entry["vision"] = bool(vis)
                # Created timestamp (epoch seconds)
                created = item.get("created")
                if isinstance(created, int):
                    entry["created_at"] = created
                    entry["created_human"] = time.strftime(
                        "%Y-%m-%d", time.gmtime(created)
                    )
                parsed.append(entry)
            result["models"] = parsed
            result["model_count"] = len(parsed)
            result["fetched_at"] = int(time.time())
        else:
            # Not all endpoints support /v1/models. Fall back to a
            # smoke test if it looks like an Anthropic-shaped endpoint.
            if provider in ("anthropic", "openrouter"):
                result["models"] = []
                result["model_count"] = 0
                result["note"] = (
                    "This endpoint does not expose /v1/models. "
                    "Try entering a known model id manually."
                )
                # Smoke test: 1-token request
                try:
                    body = {
                        "model": "claude-haiku-4-5",
                        "max_tokens": 1,
                        "messages": [{"role": "user", "content": "hi"}],
                    }
                    smoke_headers = {
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    }
                    messages_url = base_url.rstrip("/") + "/v1/messages"
                    resp2 = await client.post(messages_url, json=body, headers=smoke_headers)
                    if resp2.status_code in (200, 400):
                        result["connectivity_ok"] = True
                        result["note"] = (
                            "API key valid. Enter known model ids manually."
                        )
                    else:
                        result["connectivity_ok"] = False
                        result["note"] = f"API key rejected (HTTP {resp2.status_code})"
                except Exception as _e:
                    result["connectivity_ok"] = False
                    result["note"] = f"Connection failed: {str(_e)[:200]}"
            else:
                result["models"] = []
                result["model_count"] = 0
                result["connectivity_ok"] = True
                result["note"] = (
                    f"/v1/models returned HTTP {resp.status_code}. "
                    "This endpoint may not support model listing."
                )
    except httpx.ConnectError as exc:
        result["ok"] = False
        result["error"] = f"Connection refused: {exc}"
        result["models"] = []
        result["model_count"] = 0
    except httpx.TimeoutException as exc:
        result["ok"] = False
        result["error"] = f"Connection timed out: {exc}"
        result["models"] = []
        result["model_count"] = 0
    except Exception as exc:
        result["ok"] = False
        result["error"] = f"Discovery failed: {exc}"
        result["models"] = []
        result["model_count"] = 0

    result["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return JSONResponse(result)


@router.post("/apply")
async def apply_discovered_models(
    request: Request,
) -> JSONResponse:
    """Bulk-create profiles from discovered models.

    Body::

        {
          "endpoint_id": "abc123",
          "base_url": "https://api.openai.com/v1",
          "api_key": "sk-...",
          "provider": "openai",
          "models": ["gpt-4o", "gpt-4o-mini", "o3"],
          "options": {
            "max_tokens": 8192,
            "context_length": null,
            "prompt_cache_enabled": null,
            "extended_thinking": false
          }
        }

    Creates one profile per selected model. Profile IDs are generated
    from the pattern: ``{endpoint_id}_{model_slug}``.
    """
    payload = await request.json()
    base_url = str(payload.get("base_url") or "").strip()
    api_key = str(payload.get("api_key") or "").strip()
    provider = str(payload.get("provider") or "openai_compat").strip().lower()
    models = payload.get("models")
    if not isinstance(models, list) or not models:
        return JSONResponse(
            {"ok": False, "error": "models array is required and must be non-empty"},
            status_code=400,
        )

    options = payload.get("options") or {}

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

    endpoint_id = _endpoint_id(base_url, api_key)
    created: list[dict[str, Any]] = []

    for model_id in models:
        mid = str(model_id).strip()
        if not mid:
            continue
        # Slug: strip provider prefix (e.g. "anthropic/claude-sonnet-4" → "claude-sonnet-4")
        slug = mid.rsplit("/", 1)[-1]
        # Sanitize: lowercase, replace non-alphanumeric with -, remove consecutive -
        slug_id = re.sub(r"[^a-z0-9-]+", "-", slug.lower()).strip("-")
        if not slug_id:
            continue
        pid = f"{endpoint_id}_{slug_id}"
        if not _VALID_ID.match(pid):
            continue

        # Generate a human-friendly label
        label = mid.rsplit("/", 1)[-1]

        entry: dict[str, Any] = {
            "id": pid,
            "label": label,
            "provider": provider,
            "model": mid,
            "base_url": base_url,
        }

        # Apply optional knobs
        max_tok = options.get("max_tokens")
        if isinstance(max_tok, int) and not isinstance(max_tok, bool) and max_tok > 0:
            entry["max_tokens"] = max_tok

        ctx = options.get("context_length")
        if isinstance(ctx, int) and not isinstance(ctx, bool) and ctx > 0:
            entry["context_length"] = ctx

        pc = options.get("prompt_cache_enabled")
        if isinstance(pc, bool):
            entry["prompt_cache_enabled"] = pc

        et = options.get("extended_thinking")
        if isinstance(et, bool):
            entry["extended_thinking"] = et

        # API key: always set for discovered models
        entry["api_key"] = api_key

        # Deduplicate: replace existing profile with same id
        existing = next(
            (e for e in profiles if isinstance(e, dict) and e.get("id") == pid),
            None,
        )
        if existing is None:
            profiles.append(entry)
        else:
            idx = profiles.index(existing)
            profiles[idx] = entry

        created.append({"id": pid, "label": label, "model": mid})

    try:
        _atomic_write(target, cfg)
    except OSError as exc:
        return JSONResponse(
            {"ok": False, "error": f"write failed: {exc}"}, status_code=500
        )

    return JSONResponse({
        "ok": True,
        "endpoint_id": endpoint_id,
        "created": created,
        "restart_required": True,
    })


# ── Hot-reload: register profiles into the running daemon ───────────


@router.post("/hotload")
async def hotload_profiles(request: Request) -> JSONResponse:
    """Register one or more new profiles into the in-memory registry without restart.

    Body::

        {
          "profiles": [
            {
              "id": "endpoint1_gpt4o",
              "label": "GPT-4o",
              "provider": "openai",
              "model": "gpt-4o",
              "api_key": "sk-...",
              "base_url": "https://api.openai.com/v1"
            }
          ]
        }

    This builds LLMProvider instances on-the-fly (same logic as the
    factory) and inserts them into ``app.state.llm_registry``. The
    profiles are also persisted to ``config.json`` so they survive restarts.
    """
    payload = await request.json()
    profile_list = payload.get("profiles")
    if not isinstance(profile_list, list) or not profile_list:
        return JSONResponse(
            {"ok": False, "error": "profiles array is required"},
            status_code=400,
        )

    # Validate each profile entry first (before building anything)
    from xmclaw.daemon.factory import _PROVIDER_ORDER

    for i, prof in enumerate(profile_list):
        if not isinstance(prof, dict):
            return JSONResponse(
                {"ok": False, "error": f"profiles[{i}] must be an object"},
                status_code=400,
            )
        pid = str(prof.get("id") or "").strip()
        provider = str(prof.get("provider") or "").strip().lower()
        model = str(prof.get("model") or "").strip()
        api_key = str(prof.get("api_key") or "").strip()
        if not pid or not _VALID_ID.match(pid):
            return JSONResponse(
                {"ok": False, "error": f"profiles[{i}].id must match [a-z0-9][a-z0-9_-]{{0,63}}"},
                status_code=400,
            )
        if pid == "default":
            return JSONResponse(
                {"ok": False, "error": "id 'default' is reserved"},
                status_code=400,
            )
        if provider not in _PROVIDER_ORDER:
            return JSONResponse(
                {"ok": False, "error": f"profiles[{i}].provider must be one of {list(_PROVIDER_ORDER)}"},
                status_code=400,
            )
        if not model:
            return JSONResponse(
                {"ok": False, "error": f"profiles[{i}].model is required"},
                status_code=400,
            )
        if not api_key:
            return JSONResponse(
                {"ok": False, "error": f"profiles[{i}].api_key is required"},
                status_code=400,
            )

    # Build LLMProvider instances (re-uses factory logic)
    from xmclaw.daemon.factory import (
        _default_model_for,
        _infer_capabilities_from_model,
        _infer_tier_from_model,
        _instantiate_llm,
    )
    from xmclaw.daemon.llm_registry import LLMProfile

    created_ids: list[str] = []
    failed: list[dict[str, Any]] = []

    for i, prof in enumerate(profile_list):
        pid = str(prof.get("id") or "").strip()
        provider = str(prof.get("provider") or "").strip().lower()
        model = str(prof.get("model") or "").strip()
        api_key = str(prof.get("api_key") or "").strip()
        base_url = str(prof.get("base_url") or "").strip() or None
        label = str(prof.get("label") or "").strip() or model

        # Extract optional knobs
        raw_mt = prof.get("max_tokens")
        max_tokens: int | None = None
        if isinstance(raw_mt, int) and not isinstance(raw_mt, bool) and raw_mt > 0:
            max_tokens = raw_mt

        raw_pc = prof.get("prompt_cache_enabled")
        prompt_cache_enabled: bool | None = (
            bool(raw_pc) if isinstance(raw_pc, bool) else None
        )

        raw_et = prof.get("extended_thinking")
        extended_thinking = bool(raw_et) if isinstance(raw_et, bool) else False

        # Build the provider — for openai_compat, base_url is required
        if provider == "openai_compat" and not base_url:
            failed.append({"id": pid, "error": "openai_compat requires base_url"})
            continue

        llm = _instantiate_llm(
            provider,
            api_key=api_key,
            model=model,
            base_url=base_url,
            prompt_cache_enabled=prompt_cache_enabled,
            max_tokens=max_tokens,
            extended_thinking=extended_thinking,
        )
        if llm is None:
            failed.append({"id": pid, "error": f"provider {provider} not supported"})
            continue

        # Phase 11: capability inference for hot-loaded profiles.
        # Discovery / Apply flow doesn't carry an explicit caps list,
        # so we always derive from the model name + provider.
        caps = _infer_capabilities_from_model(model, provider=provider)
        profile_obj = LLMProfile(
            id=pid,
            label=label,
            provider_name=provider,
            model=model,
            llm=llm,
            tier=_infer_tier_from_model(model),
            capabilities=caps,
        )

        # Insert into registry
        registry = getattr(request.app.state, "llm_registry", None)
        if registry is not None:
            registry.profiles[pid] = profile_obj
            # If this is the first profile and no default yet, make it default
            if registry.default_id is None:
                registry.default_id = pid
            created_ids.append(pid)
        else:
            failed.append({"id": pid, "error": "no in-memory registry available"})

    # Persist to config.json as well
    target = _config_path(request)
    if target is None:
        return JSONResponse({
            "ok": True,
            "hotloaded": created_ids,
            "failed": failed,
            "note": "profiles registered in-memory but config_path unavailable for persistence",
        }, status_code=207)

    try:
        cfg = _load_config(target)
        llm = cfg.setdefault("llm", {})
        if not isinstance(llm, dict):
            llm = {}
            cfg["llm"] = llm
        profiles = llm.setdefault("profiles", [])
        if not isinstance(profiles, list):
            profiles = []
            llm["profiles"] = profiles

        for pid in created_ids:
            prof_entry = next(p for p in profile_list if str(p.get("id")) == pid)
            existing = next(
                (e for e in profiles if isinstance(e, dict) and e.get("id") == pid),
                None,
            )
            if existing is None:
                profiles.append(prof_entry)
            else:
                idx = profiles.index(existing)
                profiles[idx] = prof_entry

        _atomic_write(target, cfg)
    except Exception as exc:  # noqa: BLE001
        pass  # persistence failure doesn't invalidate the hot-load

    if not created_ids and failed:
        return JSONResponse(
            {"ok": False, "error": "all profiles failed", "failed": failed},
            status_code=400,
        )

    return JSONResponse({
        "ok": True,
        "hotloaded": created_ids,
        "failed": failed,
    })