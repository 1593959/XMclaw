"""OpenRouterLLM — first-class wrapper around OpenRouter's OpenAI-compat API.

OpenRouter (https://openrouter.ai) aggregates 200+ LLMs behind a single
OpenAI-compatible endpoint at ``https://openrouter.ai/api/v1``. Pre-B-386,
users could only reach it by setting ``llm.openai.base_url`` manually —
which works, but lost two things:

1. **Discoverability.** Peer products (OpenClaw, Hermes, CoPaw) list
   OpenRouter as a 1st-class provider in their settings UI. Burying it
   under "OpenAI-compat" was a commodity gap our users kept asking about.
2. **Attribution headers.** OpenRouter docs request ``HTTP-Referer`` and
   ``X-Title`` on every request so the call shows up under the correct
   app on the user's OpenRouter dashboard. Without them we'd appear as
   "anonymous" — fine for credits, bad for support / debugging.

Implementation: ``OpenRouterLLM`` is a thin subclass of :class:`OpenAILLM`
that swaps in the OpenRouter base_url, attaches the attribution headers
on the underlying ``AsyncOpenAI`` client, and picks a sensible default
model (``anthropic/claude-sonnet-4``). Streaming, tool-call decoding,
prompt-cache detection, and pricing all flow through the parent — anti-
req #11 says we keep the provider layer thin.

Prompt-cache auto-detect: OpenRouter routes to the underlying provider,
so the upstream's caching support is what matters. Default
``prompt_cache_enabled=None`` → pick by model prefix (``anthropic/`` or
``openai/`` → True; everything else → False, since we'd be guessing
about a third-party shim's strict-schema posture).

Pricing: OpenRouter model ids are ``<provider>/<model>`` (e.g.
``anthropic/claude-sonnet-4``). The substring matcher in
:func:`xmclaw.utils.cost.lookup_pricing` already finds these (the
``claude-sonnet`` pattern matches inside ``anthropic/claude-sonnet-4``)
so cost tracking works without a parallel lookup table.
"""
from __future__ import annotations

from typing import Any

from xmclaw.providers.llm.openai import OpenAILLM
from xmclaw.providers.llm.base import Pricing


# OpenRouter's recommended default endpoint — OpenAI-compat shim.
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

# Sensible default — Claude Sonnet 4 is the 2026-Q2 sweet spot for
# coding agents on OpenRouter (good price/quality, broad tool support).
# Users can override per-config or per-profile.
DEFAULT_MODEL = "anthropic/claude-sonnet-4"

# OpenRouter requests these headers for credit/attribution. Sent on
# every outbound request via the AsyncOpenAI client's default_headers.
DEFAULT_HTTP_REFERER = "https://github.com/1593959/XMclaw"
DEFAULT_X_TITLE = "XMclaw"


def _default_prompt_cache_for_openrouter(model: str) -> bool:
    """Pick a prompt-cache default based on the OpenRouter model prefix.

    OpenRouter routes to the underlying provider — so what matters is
    whether the upstream honours the Anthropic-style ``cache_control``
    marker on its OpenAI-compat shim.

    * ``anthropic/...`` → True (Anthropic's native prompt caching is
      well-supported via OpenRouter's pass-through).
    * ``openai/...`` → True (OpenAI auto-caches and reports
      ``prompt_tokens_details.cached_tokens``; the marker is harmless
      since OpenRouter's shim swallows unknown fields it doesn't
      recognise).
    * Everything else → False (conservative — third-party shims may
      reject unknown body fields with 400).
    """
    name = (model or "").lower()
    if name.startswith("anthropic/") or name.startswith("openai/"):
        return True
    return False


class OpenRouterLLM(OpenAILLM):
    """OpenRouter LLM provider — OpenAI-compat with attribution headers.

    Parameters
    ----------
    api_key : str
        OpenRouter API key (``sk-or-v1-...``).
    model : str
        OpenRouter model id (``<provider>/<model>``, e.g.
        ``"anthropic/claude-sonnet-4"``). Defaults to
        :data:`DEFAULT_MODEL`.
    base_url : str | None
        Override when self-hosting an OpenRouter-compat gateway.
        Defaults to :data:`DEFAULT_BASE_URL`.
    pricing : Pricing | None
        Per-million-token USD pricing override. When unset, the
        ``.pricing`` property delegates to ``lookup_pricing(model)`` —
        the substring matcher already handles OpenRouter's
        ``provider/model`` ids (e.g. ``anthropic/claude-sonnet-4`` →
        the ``claude-sonnet`` pattern → 3.0 / 15.0).
    prompt_cache_enabled : bool | None
        ``True`` / ``False`` overrides; ``None`` (default) picks via
        :func:`_default_prompt_cache_for_openrouter` — on for
        ``anthropic/`` and ``openai/`` prefixes, off otherwise.
    http_referer : str
        Value for the ``HTTP-Referer`` attribution header. Defaults to
        the XMclaw repo URL.
    x_title : str
        Value for the ``X-Title`` attribution header. Defaults to
        ``"XMclaw"``.
    """

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        base_url: str | None = None,
        pricing: Pricing | None = None,
        *,
        prompt_cache_enabled: bool | None = None,
        http_referer: str = DEFAULT_HTTP_REFERER,
        x_title: str = DEFAULT_X_TITLE,
    ) -> None:
        # Auto-pick cache default before super().__init__ so OpenAILLM's
        # own auto-detect (which keys off base_url + model substrings
        # like "moonshot" / "kimi" / "glm") doesn't fire — those hints
        # are meaningless for OpenRouter, which always lives at
        # openrouter.ai regardless of the underlying model.
        if prompt_cache_enabled is None:
            prompt_cache_enabled = _default_prompt_cache_for_openrouter(model)

        super().__init__(
            api_key=api_key,
            model=model,
            base_url=base_url or DEFAULT_BASE_URL,
            pricing=pricing,
            prompt_cache_enabled=prompt_cache_enabled,
        )
        self._http_referer = http_referer
        self._x_title = x_title

    def _get_client(self) -> Any:
        """Override OpenAILLM's lazy client to attach OpenRouter
        attribution headers via ``default_headers``.

        ``HTTP-Referer`` and ``X-Title`` ride on every outbound request
        so calls show up under "XMclaw" on the user's OpenRouter
        dashboard. Without them we'd be tagged as "anonymous".
        """
        if self._client is not None:
            return self._client
        from openai import AsyncOpenAI
        kwargs: dict[str, Any] = {
            "api_key": self.api_key,
            "default_headers": {
                "HTTP-Referer": self._http_referer,
                "X-Title": self._x_title,
            },
        }
        if self.base_url:
            kwargs["base_url"] = self.base_url
        self._client = AsyncOpenAI(**kwargs)
        return self._client
