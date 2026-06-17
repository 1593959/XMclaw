"""Surface vendor API error bodies instead of bare status codes.

2026-06-17. ``httpx`` ``raise_for_status()`` throws a generic
``HTTPStatusError`` that says only "404 Not Found" — it drops the JSON
error body the vendor actually returned. For media/voice backends that
matters: a Volcengine Ark video POST that 404s with
``UnsupportedModel: The requested model does not support the agent plan
feature`` is a clear, actionable message; surfacing only "(404)" made the
agent think the service was down and silently fabricate a fake animation.

Shared by ``providers/media`` and ``providers/voice`` (both may import
``utils``).
"""
from __future__ import annotations

from typing import Any

__all__ = ["vendor_error_message", "raise_for_vendor_error"]


def vendor_error_message(resp: Any) -> str:
    """Best-effort human-readable error from a vendor response body.

    Handles OpenAI-shape ``{"error": {"code","message"}}``, flat
    ``{"message"|"msg"|"detail"}``, and MiniMax ``{"base_resp":
    {"status_code","status_msg"}}``. Falls back to the raw text."""
    try:
        j = resp.json()
    except Exception:  # noqa: BLE001
        return (getattr(resp, "text", "") or "")[:300]

    if isinstance(j, dict):
        err = j.get("error")
        if isinstance(err, dict):
            code = err.get("code")
            msg = err.get("message") or err.get("msg")
            if msg:
                return f"{code}: {msg}" if code else str(msg)
        if isinstance(err, str) and err:
            return err
        for k in ("message", "msg", "detail"):
            v = j.get(k)
            if isinstance(v, str) and v:
                return v
        br = j.get("base_resp")
        if isinstance(br, dict) and br.get("status_msg"):
            return f"{br.get('status_code')}: {br.get('status_msg')}"
    return (getattr(resp, "text", "") or "")[:300]


def raise_for_vendor_error(resp: Any, context: str) -> None:
    """Raise ``RuntimeError`` with the vendor's error message when ``resp``
    is a 4xx/5xx. No-op on success. Use in place of ``raise_for_status()``
    where a clear, surfaced message matters."""
    status = int(getattr(resp, "status_code", 0) or 0)
    if status >= 400:
        raise RuntimeError(
            f"{context} failed (HTTP {status}): {vendor_error_message(resp)}"
        )
