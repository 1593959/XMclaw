"""ComposioToolProvider — bridge XMclaw to Composio's 7000+ pre-integrated tools.

B-389 (Sprint 2). Composio (https://composio.dev) is a tool-aggregation
service that pre-builds OAuth flows + thin function wrappers for hundreds
of popular SaaS apps (Gmail, Slack, GitHub, Notion, Linear, HubSpot, …).
Their Python SDK exposes each app as a bundle of "actions" with stable
names like ``GMAIL_SEND_EMAIL`` / ``SLACK_SENDMESSAGE`` / ``GITHUB_CREATE_ISSUE``
plus an OpenAI-shape function-spec for each. This adapter wraps the SDK
as a :class:`~xmclaw.providers.tool.base.ToolProvider` so the agent loop
sees Composio actions alongside builtin tools, MCP-bridged tools, etc.

Wiring:

    cfg = {
        "tools": {
            "composio": {
                "enabled": True,
                "api_key": "ck_live_...",   # https://app.composio.dev → Settings → API keys
                "entity_id": "default",     # OAuth identity; usually "default"
                "apps": ["GMAIL", "SLACK", "GITHUB", "NOTION"],
                "cache_ttl_s": 300          # optional, default 300
            }
        }
    }

The bridge is **lazy**: ``import composio`` happens only when ``list_tools()``
or ``invoke()`` actually needs the SDK. Importing this module alone (e.g.
during daemon boot when ``tools.composio.enabled`` is False) does NOT
require ``composio_core`` to be installed. A missing SDK surfaces as a
structured ``ToolResult(ok=False, error="...install...")`` at first call,
or — for the configured-but-disabled state — as a clean skip in the
factory.

Auth flow (NOT handled here):
    Composio uses per-user OAuth for many apps. Authorising an entity
    against Gmail / Slack / GitHub is a CLI / dashboard step the user
    performs separately at https://app.composio.dev (or via the
    ``composio`` CLI). This bridge ONLY consumes already-authorised
    entities — it never initiates OAuth, captures redirect URIs, or
    handles refresh tokens. If an action returns "no integration
    configured for this entity", the failure is surfaced verbatim so
    the user knows to authorise the missing app.

Caching:
    ``list_tools()`` is hit once per turn by the agent loop, but the
    underlying ``ComposioToolSet.get_tools(...)`` does a network round-
    trip per call. We cache the converted ``ToolSpec`` list with a
    configurable TTL (default 5 min) — long enough to avoid burning
    quota on every turn, short enough that adding a new Composio app to
    your account becomes visible without a daemon restart.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from xmclaw.core.ir import ToolCall, ToolResult, ToolSpec
from xmclaw.providers.tool.base import ToolProvider


_INSTALL_HINT = (
    "ComposioToolProvider needs the ``composio-core`` package. "
    "Install with: pip install 'xmclaw[tools-composio]' "
    "(or: pip install composio-core>=0.7)"
)

# Default TTL for the tool-list cache. 5 min is the sweet spot:
# long enough that we don't refetch on every conversational turn,
# short enough that a freshly-authorised app shows up the next time
# the agent reflects on its toolset.
_DEFAULT_CACHE_TTL_S = 300.0


class ComposioToolProvider(ToolProvider):
    """A :class:`ToolProvider` backed by the Composio tool-aggregation SDK.

    Parameters
    ----------
    api_key : str
        Composio API key (``ck_live_...`` or ``ck_test_...``). Get one at
        https://app.composio.dev → Settings → API keys. Required — empty
        / whitespace-only raises :class:`ValueError` at construction time
        so the daemon factory fails fast on a half-filled config.
    entity_id : str
        Composio "entity" identifier. An entity is the per-user
        authorisation scope: in a single-tenant XMclaw install this is
        usually ``"default"``; for multi-user deployments it would be
        the user id. See https://docs.composio.dev/concepts/entities.
    apps : list[str]
        Whitelist of Composio app slugs (e.g. ``["GMAIL", "SLACK",
        "GITHUB", "NOTION"]``). Only actions belonging to these apps are
        exposed to the agent. Empty list -> no Composio tools surface
        (effectively disabled). Apps the user hasn't authorised in
        Composio dashboard simply have zero actions returned by the SDK
        and are skipped silently.
    cache_ttl_s : float
        How long to cache the converted ``ToolSpec`` list before the
        next ``list_tools()`` call refetches from Composio. 0 / negative
        disables caching (refetch every call — useful for tests). Default
        300s.
    """

    def __init__(
        self,
        api_key: str,
        *,
        entity_id: str = "default",
        apps: list[str] | None = None,
        cache_ttl_s: float = _DEFAULT_CACHE_TTL_S,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError(
                "ComposioToolProvider requires a non-empty api_key. "
                "Get one at https://app.composio.dev or set "
                "config.tools.composio.api_key (or env "
                "XMC__tools__composio__api_key)."
            )
        self._api_key = api_key.strip()
        self._entity_id = (entity_id or "default").strip() or "default"
        # Normalise app slugs: Composio convention is upper-snake-case
        # (GMAIL, SLACK_BOT, GOOGLE_DRIVE, …). Strip / upper preserves
        # user intent without forcing them to write the exact form.
        self._apps = tuple(
            (a or "").strip().upper()
            for a in (apps or [])
            if isinstance(a, str) and (a or "").strip()
        )
        self._cache_ttl_s = max(0.0, float(cache_ttl_s))
        self._cached_specs: list[ToolSpec] | None = None
        self._cached_at: float = 0.0
        # Lazy: the SDK toolset object. Created on first list_tools /
        # invoke call so a daemon that wires Composio in config but
        # never actually receives a tool call doesn't pay the import cost.
        self._toolset: Any = None

    # ── ToolProvider contract ───────────────────────────────────────

    def list_tools(self) -> list[ToolSpec]:
        """Return Composio actions visible to the agent as ToolSpecs.

        Cached per ``cache_ttl_s``. SDK / network errors surface as an
        empty list (not an exception): callers — especially the
        post-construction wiring in factory.py and the system-prompt
        builder — must keep functioning even when Composio is
        misconfigured. The error is re-raised through ``invoke()``
        when the agent actually tries to call one of these tools, so
        the user sees a clear message in tool output rather than a
        cold daemon-boot crash.
        """
        now = time.monotonic()
        if (
            self._cached_specs is not None
            and self._cache_ttl_s > 0
            and (now - self._cached_at) < self._cache_ttl_s
        ):
            return list(self._cached_specs)
        try:
            specs = self._fetch_specs()
        except Exception:  # noqa: BLE001 — see docstring rationale
            specs = []
        self._cached_specs = specs
        self._cached_at = now
        return list(specs)

    async def invoke(self, call: ToolCall) -> ToolResult:
        """Run one Composio action.

        The agent has called ``call.name`` (e.g. ``GMAIL_SEND_EMAIL``)
        with ``call.args``. We hand both to the SDK and translate its
        response back into our :class:`ToolResult` shape. Errors
        (auth fail, action not found, rate limited, network blip) all
        come back as ``ToolResult(ok=False, error=...)`` with a
        classified prefix so the LLM can see the failure mode.

        The SDK is sync + IO-bound (HTTP under the hood) — we run it
        on a worker thread to avoid stalling the daemon's event loop.
        """
        try:
            toolset = self._get_toolset()
        except ImportError as exc:
            return ToolResult(
                call_id=call.id, ok=False, content=None,
                error=f"composio_unavailable: {exc}",
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                call_id=call.id, ok=False, content=None,
                error=f"composio_init_error: {type(exc).__name__}: {exc}",
            )

        def _run() -> Any:  # noqa: ANN401 — SDK return shape varies
            # Composio's ``execute_action`` accepts (action, params,
            # entity_id) and returns a dict like:
            #   {"successful": bool, "data": Any, "error": str | None}
            # Older SDK versions and some actions return raw scalars;
            # _response_to_tool_result handles both.
            return toolset.execute_action(
                action=call.name,
                params=dict(call.args or {}),
                entity_id=self._entity_id,
            )

        try:
            response = await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                call_id=call.id, ok=False, content=None,
                error=_classify_error(exc),
            )

        return _response_to_tool_result(call.id, response)

    # ── internals ───────────────────────────────────────────────────

    def _get_toolset(self) -> Any:
        """Lazily build the underlying ``ComposioToolSet``.

        Raises :class:`ImportError` with a pip-install hint when
        ``composio`` is missing; raises whatever the SDK raises when
        construction fails (typically auth / config errors). Caches
        the constructed toolset so subsequent calls are free.
        """
        if self._toolset is not None:
            return self._toolset
        try:
            # The PyPI package is ``composio-core``; the import name is
            # ``composio``. Both ``ComposioToolSet`` (legacy) and
            # ``Composio`` (newer surface) exist on recent versions. We
            # prefer the documented ``ComposioToolSet`` which has the
            # stable get_tools / execute_action methods.
            from composio import ComposioToolSet  # type: ignore
        except ImportError as exc:
            raise ImportError(_INSTALL_HINT) from exc
        self._toolset = ComposioToolSet(
            api_key=self._api_key,
            entity_id=self._entity_id,
        )
        return self._toolset

    def _fetch_specs(self) -> list[ToolSpec]:
        """Fetch Composio's action list and convert each to ToolSpec.

        Composio's ``get_tools`` returns OpenAI-shape function specs:
          [{"type": "function", "function": {
              "name": "GMAIL_SEND_EMAIL",
              "description": "...",
              "parameters": {<JSON Schema>}}}, ...]

        We unwrap the inner ``function`` block and map each to our
        frozen :class:`ToolSpec`. Empty ``apps`` list -> empty result
        (no Composio tools surface).
        """
        if not self._apps:
            return []
        toolset = self._get_toolset()
        # The SDK accepts ``apps=[<slug>, ...]`` as strings; passing the
        # ``App`` enum is also valid but pulls in their enum class which
        # we don't want to bind to. Strings are forward-compatible: new
        # apps Composio adds in the future work without an SDK upgrade.
        raw = toolset.get_tools(apps=list(self._apps))
        specs: list[ToolSpec] = []
        for entry in raw or []:
            spec = _convert_openai_function_to_toolspec(entry)
            if spec is not None:
                specs.append(spec)
        return specs


# ── pure helpers (testable without an SDK) ─────────────────────────────


def _convert_openai_function_to_toolspec(entry: Any) -> ToolSpec | None:
    """Translate one OpenAI-shape function spec to a XMclaw :class:`ToolSpec`.

    Composio (and most modern tool aggregators) returns the OpenAI
    Chat Completions function-calling shape::

        {"type": "function",
         "function": {"name": "...", "description": "...",
                      "parameters": {<JSON Schema>}}}

    Some SDK versions / call paths return the ``function`` block flat
    (no outer wrapper). We accept both. Anything malformed → return
    None so the caller skips it; we never raise on a single bad entry
    because Composio occasionally ships tools with quirky schemas and
    losing the whole catalogue over one bad row is the wrong tradeoff.
    """
    if not isinstance(entry, dict):
        return None
    # Some SDK versions wrap, others don't.
    fn_block = entry.get("function") if "function" in entry else entry
    if not isinstance(fn_block, dict):
        return None
    name = fn_block.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    desc = fn_block.get("description") or ""
    if not isinstance(desc, str):
        desc = str(desc)
    params = fn_block.get("parameters")
    if not isinstance(params, dict):
        params = {"type": "object", "properties": {}}
    return ToolSpec(
        name=name.strip(),
        description=desc,
        parameters_schema=params,
    )


def _response_to_tool_result(call_id: str, response: Any) -> ToolResult:
    """Translate a Composio ``execute_action`` response to a ToolResult.

    Composio's return shape (post-2024 SDK) is::

        {"successful": True/False,
         "data": <Any>,             # tool's actual output
         "error": "...",            # populated when successful=False
         "error_code": "..."}

    Older versions used ``"success"`` instead of ``"successful"`` —
    we accept both. Anything that doesn't look like a dict gets
    wrapped as a plain successful result with the raw response as
    content (best-effort: a tool that returns a string is still
    useful even if the SDK skipped its envelope).
    """
    if not isinstance(response, dict):
        return ToolResult(
            call_id=call_id, ok=True, content=response,
            side_effects=(),
        )
    successful = response.get("successful")
    if successful is None:
        successful = response.get("success", True)
    if not successful:
        err = response.get("error") or response.get("error_message") or "unknown error"
        code = response.get("error_code")
        prefix = f"composio_action_failed[{code}]" if code else "composio_action_failed"
        return ToolResult(
            call_id=call_id, ok=False, content=None,
            error=f"{prefix}: {err}",
        )
    data = response.get("data") if "data" in response else response
    return ToolResult(
        call_id=call_id, ok=True, content=data, side_effects=(),
    )


def _classify_error(exc: BaseException) -> str:
    """Map a raw SDK exception to a stable ``error`` string.

    The agent's LLM consumes ``ToolResult.error`` directly. A stable
    prefix (``composio_auth_error:`` / ``composio_rate_limited:`` /
    ``composio_action_not_found:`` / ``composio_network_error:`` /
    ``composio_error:``) helps the model decide whether to retry,
    re-prompt for credentials, or fall back to a builtin. Heuristic
    string-match — Composio doesn't ship a structured exception
    hierarchy (yet).
    """
    name = type(exc).__name__
    msg = str(exc) or name
    lowered = msg.lower()
    if "401" in msg or "unauthorized" in lowered or "auth" in lowered:
        return f"composio_auth_error: {msg}"
    if "429" in msg or "rate limit" in lowered or "too many" in lowered:
        return f"composio_rate_limited: {msg}"
    if "not found" in lowered or "404" in msg or "no such action" in lowered:
        return f"composio_action_not_found: {msg}"
    if "timeout" in lowered or "network" in lowered or "connection" in lowered:
        return f"composio_network_error: {msg}"
    return f"composio_error: {name}: {msg}"
