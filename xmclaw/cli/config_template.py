"""Shared minimum-viable daemon/config.json template.

Single source of truth for both ``xmclaw config init`` and
:class:`xmclaw.cli.doctor_registry.ConfigCheck`'s auto-fix. Kept as a
Python literal (not a read from ``daemon/config.example.json``) so
the pip-installed wheel works the same as a source checkout --
``daemon/config.example.json`` lives at the repo root and is not
bundled into the package.

The example JSON stays the canonical documentation of every optional
section (tools/memory/evolution/integrations). This literal is the
minimum the daemon needs to boot: ``llm``, ``gateway``, and the
``security.prompt_injection`` policy switch.
"""
from __future__ import annotations

from typing import Any


def default_config_template() -> dict[str, Any]:
    """Fresh dict on every call -- callers may mutate to inject api keys."""
    return {
        "llm": {
            "default_provider": "anthropic",
            "anthropic": {
                "api_key": "",
                "base_url": "https://api.anthropic.com",
                "default_model": "",
            },
            "openai": {
                "api_key": "",
                "base_url": "https://api.openai.com/v1",
                "default_model": "",
            },
        },
        "gateway": {"host": "127.0.0.1", "port": 8765},
        "security": {"prompt_injection": "detect_only"},
    }
