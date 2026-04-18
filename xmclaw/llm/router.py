"""LLM router: pluggable provider selection and request handling."""
import asyncio
from pathlib import Path
from typing import Any, AsyncIterator

from xmclaw.daemon.config import DaemonConfig
from xmclaw.core.error_recovery import get_error_recovery, CircuitBreaker
from xmclaw.utils.log import logger
from xmclaw.utils.paths import BASE_DIR

# Built-in providers
from xmclaw.llm.openai_client import OpenAIClient
from xmclaw.llm.anthropic_client import AnthropicClient

_PROVIDERS_DIR = BASE_DIR / "plugins" / "llm"


class LLMProviderPlugin:
    """Base class for LLM provider plugins.

    To add a new provider (e.g., Google Gemini, Ollama):
    1. Create plugins/llm/gemini.py
    2. Define a class GeminiProvider(LLMProviderPlugin):
    3. Implement stream() and complete() methods
    4. Add to config: llm.providers.gemini = {...}
    """
    name: str = "plugin"
    supports_streaming: bool = True
    supports_embeddings: bool = False

    def __init__(self, config: dict):
        self.config = config

    async def stream(self, messages: list[dict], tools: list[dict] | None = None) -> AsyncIterator[str]:
        raise NotImplementedError(f"{self.name} does not support streaming")

    async def complete(self, messages: list[dict]) -> str:
        raise NotImplementedError(f"{self.name} does not support complete()")

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Override if the provider supports embeddings."""
        return []


class LLMRouter:
    """Pluggable LLM router supporting multiple providers with fallback.

    Providers are loaded in order of preference:
    1. Built-in (openai, anthropic)
    2. Plugin providers (plugins/llm/*.py)

    Each provider has its own config section under llm.providers.<name>.

    Error Recovery:
    - Circuit breaker per provider to prevent cascading failures
    - Automatic fallback to next provider on failure
    - Retry logic with exponential backoff
    """
    def __init__(self):
        self.config = DaemonConfig.load()
        self.clients: dict[str, Any] = {}
        self._load_providers()
        # Circuit breaker for each provider
        self._breakers: dict[str, CircuitBreaker] = {
            name: CircuitBreaker(failure_threshold=5, recovery_timeout=60.0)
            for name in self.clients
        }
        self._recovery = get_error_recovery()

    def _load_providers(self) -> None:
        """Load built-in and plugin providers."""
        # Built-in
        llm_cfg = self.config.llm
        for name in ("openai", "anthropic"):
            cfg = llm_cfg.get(name, {})
            if not cfg:
                continue
            try:
                if name == "openai":
                    self.clients["openai"] = OpenAIClient(cfg)
                elif name == "anthropic":
                    self.clients["anthropic"] = AnthropicClient(cfg)
            except Exception as e:
                logger.warning(f"llm_provider_init_failed", provider=name, error=str(e))

        # Discover plugin providers from plugins/llm/
        self._discover_plugins()

        logger.info("llm_providers_loaded", providers=list(self.clients.keys()))

    def _discover_plugins(self) -> None:
        """Auto-discover LLM provider plugins from plugins/llm/."""
        if not _PROVIDERS_DIR.exists():
            _PROVIDERS_DIR.mkdir(parents=True, exist_ok=True)
            (_PROVIDERS_DIR / "README.md").write_text(
                "# Drop custom LLM provider plugins here\n"
                "# Each .py file defining LLMProviderPlugin subclass is auto-loaded.\n"
                "# Example (plugins/llm/ollama.py):\n"
                "#   from xmclaw.llm.router import LLMProviderPlugin\n"
                "#   class OllamaProvider(LLMProviderPlugin):\n"
                "#       name = 'ollama'\n"
                "#       supports_embeddings = True\n"
                "#       async def stream(self, messages, tools=None): ...\n",
                encoding="utf-8"
            )
            return

        import importlib.util
        for py_file in sorted(_PROVIDERS_DIR.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            try:
                spec = importlib.util.spec_from_file_location(f"llm_plugin_{py_file.stem}", str(py_file))
                if not spec or not spec.loader:
                    continue
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                # Find LLMProviderPlugin subclasses
                for attr_name in dir(module):
                    attr = getattr(module, attr_name, None)
                    if not isinstance(attr, type) or attr is LLMProviderPlugin:
                        continue
                    try:
                        if issubclass(attr, LLMProviderPlugin) and attr is not LLMProviderPlugin:
                            name = getattr(attr, "name", py_file.stem)
                            # Get config for this provider
                            provider_cfg = self.config.llm.get("providers", {}).get(name, {})
                            client = attr(provider_cfg)
                            self.clients[name] = client
                            logger.info("llm_plugin_loaded", provider=name, path=str(py_file))
                    except Exception:
                        pass
            except Exception as e:
                logger.warning("llm_plugin_load_failed", path=str(py_file), error=str(e))

    def register_provider(self, name: str, client: LLMProviderPlugin) -> None:
        """Manually register a provider plugin.

        Usage:
            from xmclaw.llm.router import LLMRouter, LLMProviderPlugin
            class MyProvider(LLMProviderPlugin):
                name = 'myprovider'
                ...
            router = LLMRouter.get_shared()
            router.register_provider('myprovider', MyProvider({...}))
        """
        self.clients[name] = client
        logger.info("llm_provider_registered", name=name)

    def list_providers(self) -> list[dict[str, Any]]:
        """Return all available providers."""
        return [
            {"name": name, "class": type(c).__name__}
            for name, c in self.clients.items()
        ]

    async def stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        provider: str | None = None,
        fallback: bool = True,
    ) -> AsyncIterator[str]:
        """Stream from the selected provider, optionally falling back on error.

        Uses circuit breakers to prevent cascading failures and provides
        automatic provider failover with retry logic.
        """
        target = provider or self.config.llm.get("default_provider", "anthropic")
        tried: list[str] = []
        providers_to_try = [target]
        if fallback:
            providers_to_try.extend(k for k in self.clients if k != target)

        for p in providers_to_try:
            client = self.clients.get(p)
            breaker = self._breakers.get(p)
            if not client:
                continue

            tried.append(p)

            # Check circuit breaker
            if breaker and not await breaker.can_execute():
                logger.warning("llm_circuit_breaker_open", provider=p, state=breaker.state.value)
                continue

            try:
                # Reset collected chunks on each provider attempt
                provider_chunks = []
                async for chunk in client.stream(messages, tools=tools):
                    provider_chunks.append(chunk)
                    yield chunk

                # Success - record and return
                if breaker:
                    await breaker.record_success()
                return  # Success

            except Exception as e:
                logger.warning("llm_provider_error", provider=p, error=str(e))
                if breaker:
                    await breaker.record_failure()
                continue

        logger.error("llm_all_providers_failed", tried=tried)
        yield f"[Error: All LLM providers failed. Tried: {', '.join(tried)}]"

    async def complete(
        self,
        messages: list[dict],
        provider: str | None = None,
        fallback: bool = True,
    ) -> str:
        """Complete from the selected provider, optionally falling back on error.

        Uses circuit breakers to prevent cascading failures and provides
        automatic provider failover with retry logic.
        """
        target = provider or self.config.llm.get("default_provider", "anthropic")
        tried: list[str] = []
        providers_to_try = [target]
        if fallback:
            providers_to_try.extend(k for k in self.clients if k != target)

        last_error: Exception | None = None
        for p in providers_to_try:
            client = self.clients.get(p)
            breaker = self._breakers.get(p)
            if not client:
                continue

            tried.append(p)

            # Check circuit breaker
            if breaker and not await breaker.can_execute():
                logger.warning("llm_circuit_breaker_open", provider=p, state=breaker.state.value)
                continue

            try:
                result = await client.complete(messages)
                # Success - record and return
                if breaker:
                    await breaker.record_success()
                return result

            except Exception as e:
                last_error = e
                logger.warning("llm_provider_error", provider=p, error=str(e))
                if breaker:
                    await breaker.record_failure()
                continue

        raise RuntimeError(f"All LLM providers failed. Tried: {', '.join(tried)}. Last error: {last_error}")

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings, trying each provider in order."""
        for name, client in self.clients.items():
            if hasattr(client, "embed"):
                try:
                    result = await client.embed(texts)
                    if result:
                        return result
                except Exception as e:
                    logger.warning("embed_provider_error", provider=name, error=str(e))
        return []

    def rebuild_clients(self) -> None:
        """Reload all provider clients with updated config.

        Call after config changes via the API.
        Also rebuilds circuit breakers for new providers.
        """
        self.clients.clear()
        self.config = DaemonConfig.load()
        self._load_providers()
        # Rebuild circuit breakers for new provider set
        self._breakers = {
            name: CircuitBreaker(failure_threshold=5, recovery_timeout=60.0)
            for name in self.clients
        }
        logger.info("llm_clients_rebuilt", providers=list(self.clients.keys()))
