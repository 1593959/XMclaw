"""VercelSkillRuntime — skill execution via Vercel serverless functions.

This runtime POSTs skill inputs to a pre-deployed Vercel function endpoint
and returns the JSON response as a ``SkillOutput``.  It is NOT a generic
"deploy anything to Vercel" runtime — it expects the endpoint to implement
the B-385 envelope contract:

  * POST body: ``{"skill_id": "...", "version": N, "args": {...}}``
  * Response: ``{"tag": "ok", "output": {...}}`` or
    ``{"tag": "skill_error", "error": "..."}``

Useful when:

  * You already run inference or tool logic on Vercel Edge / Node / Python
    serverless functions.
  * You want skills to execute geographically close to your users.
  * You want to leverage Vercel's CDN + caching for idempotent skill calls.

Security posture
----------------
The runtime sends skill args over HTTPS to the configured endpoint.  Use
a shared secret (``auth_token``) and verify it in the Vercel function to
prevent unauthorized invocation.  The runtime does NOT deploy functions —
that is the operator's responsibility.

Requires ``httpx`` (already a core XMclaw dependency).
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from xmclaw.providers.runtime.base import (
    SkillHandle,
    SkillRuntime,
    SkillStatus,
)
from xmclaw.skills.base import Skill, SkillOutput
from xmclaw.skills.manifest import SkillManifest


@dataclass
class _Slot:
    handle: SkillHandle
    manifest: SkillManifest
    task: asyncio.Task[SkillOutput] | None = None
    output: SkillOutput | None = None
    killed: bool = False
    timed_out: bool = False
    errored: bool = False


class VercelSkillRuntime(SkillRuntime):
    """Invoke skills via a Vercel serverless function endpoint.

    Parameters
    ----------
    endpoint_url : str
        Full HTTPS URL of the Vercel function to invoke.
    auth_token : str | None
        Bearer token sent in the ``Authorization`` header. Optional but
        strongly recommended.
    timeout_s : float
        HTTP request timeout (default 60.0).
    """

    def __init__(
        self,
        *,
        endpoint_url: str,
        auth_token: str | None = None,
        timeout_s: float = 60.0,
    ) -> None:
        self._endpoint_url = endpoint_url
        self._auth_token = auth_token
        self._timeout_s = float(timeout_s)
        self._slots: dict[str, _Slot] = {}

    # ── helpers ──────────────────────────────────────────────────────

    def _http_client(self) -> Any:
        try:
            import httpx  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError(
                "VercelSkillRuntime needs 'httpx'. "
                "Install with: pip install httpx"
            ) from exc
        return httpx

    # ── contract ─────────────────────────────────────────────────────

    def enforce_manifest(self, manifest: SkillManifest) -> None:
        if manifest.max_cpu_seconds < 0:
            raise ValueError(
                f"manifest.max_cpu_seconds must be >= 0, got {manifest.max_cpu_seconds}"
            )
        if manifest.max_memory_mb < 0:
            raise ValueError(
                f"manifest.max_memory_mb must be >= 0, got {manifest.max_memory_mb}"
            )

    async def fork(
        self,
        skill: Skill,
        manifest: SkillManifest,
        args: dict[str, Any],
    ) -> SkillHandle:
        self.enforce_manifest(manifest)
        if manifest.id != skill.id or manifest.version != skill.version:
            raise ValueError(
                f"manifest/skill identity mismatch: skill={skill.id}v{skill.version} "
                f"manifest={manifest.id}v{manifest.version}"
            )

        handle = SkillHandle(
            id=f"vc-{skill.id}-{manifest.version}",
            skill_id=skill.id,
            version=skill.version,
            pid=None,
        )
        slot = _Slot(handle=handle, manifest=manifest)
        self._slots[handle.id] = slot

        slot.task = asyncio.create_task(self._invoke(skill, manifest, args, slot))
        return handle

    async def _invoke(
        self,
        skill: Skill,
        manifest: SkillManifest,
        args: dict[str, Any],
        slot: _Slot,
    ) -> SkillOutput:
        httpx = self._http_client()
        payload = {
            "skill_id": skill.id,
            "version": skill.version,
            "args": args,
        }
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"

        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                resp = await client.post(
                    self._endpoint_url,
                    headers=headers,
                    json=payload,
                )
        except Exception as exc:  # noqa: BLE001
            return SkillOutput(
                ok=False,
                result={
                    "error": f"HTTP error: {type(exc).__name__}: {exc}",
                    "kind": "vercel_http_error",
                },
                side_effects=[],
            )

        if resp.status_code >= 400:
            return SkillOutput(
                ok=False,
                result={
                    "error": f"HTTP {resp.status_code}: {resp.text[:500]}",
                    "kind": "vercel_http_error",
                },
                side_effects=[],
            )

        try:
            envelope = resp.json()
        except json.JSONDecodeError as exc:
            return SkillOutput(
                ok=False,
                result={
                    "error": f"response is not valid JSON: {exc}",
                    "raw": resp.text[:500],
                    "kind": "vercel_json_error",
                },
                side_effects=[],
            )

        tag = envelope.get("tag")
        if tag == "ok":
            return SkillOutput(
                ok=True,
                result=envelope.get("output", {}),
                side_effects=[],
            )
        return SkillOutput(
            ok=False,
            result={
                "error": envelope.get("error", "unknown remote error"),
                "kind": tag or "unknown",
            },
            side_effects=[],
        )

    async def wait(
        self,
        handle: SkillHandle,
        timeout: float | None = None,
    ) -> SkillOutput:
        slot = self._get_slot(handle)
        if slot.output is not None:
            return slot.output
        if slot.task is None:
            raise LookupError(f"handle {handle.id} has no task")

        cap = slot.manifest.max_cpu_seconds or None
        effective = cap if timeout is None else (
            min(cap, timeout) if cap else timeout
        )

        try:
            if effective is not None and effective > 0:
                output = await asyncio.wait_for(
                    asyncio.shield(slot.task), timeout=effective,
                )
            else:
                output = await slot.task
            slot.output = output
            return output
        except asyncio.TimeoutError:
            slot.timed_out = True
            slot.task.cancel()
            try:
                await slot.task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            slot.output = SkillOutput(
                ok=False,
                result={
                    "error": f"timeout: skill exceeded {effective}s on Vercel",
                    "kind": "timeout",
                },
                side_effects=[],
            )
            return slot.output
        except asyncio.CancelledError:
            slot.killed = True
            slot.output = SkillOutput(
                ok=False,
                result={"error": "killed", "kind": "killed"},
                side_effects=[],
            )
            return slot.output

    async def kill(self, handle: SkillHandle) -> None:
        slot = self._get_slot(handle)
        if slot.task is None or slot.task.done():
            return
        slot.killed = True
        slot.task.cancel()
        try:
            await slot.task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    async def status(self, handle: SkillHandle) -> SkillStatus:
        slot = self._get_slot(handle)
        if slot.task is None:
            return SkillStatus.PENDING
        if not slot.task.done():
            return SkillStatus.RUNNING
        if slot.timed_out:
            return SkillStatus.TIMEOUT
        if slot.killed:
            return SkillStatus.KILLED
        if slot.errored:
            return SkillStatus.FAILED
        if slot.output is None:
            if slot.task.cancelled():
                return SkillStatus.KILLED
            exc = slot.task.exception()
            if exc is not None:
                return SkillStatus.FAILED
            return SkillStatus.SUCCEEDED
        return SkillStatus.SUCCEEDED if slot.output.ok else SkillStatus.FAILED

    # ── helpers ──────────────────────────────────────────────────────

    def _get_slot(self, handle: SkillHandle) -> _Slot:
        slot = self._slots.get(handle.id)
        if slot is None:
            raise LookupError(f"unknown handle id={handle.id!r}")
        return slot
