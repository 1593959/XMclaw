"""Onboarding wizard — Epic #9.

Interactive first-time setup: provider selection, API key capture,
workspace confirmation, tool enablement, and a smoke test.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import questionary
import typer

from xmclaw.cli.config_template import default_config_template
from xmclaw.utils.i18n import _
from xmclaw.utils.paths import data_dir
from xmclaw.utils.secrets import set_secret


_DEFAULT_CONFIG_PATH = Path("daemon/config.json")


class OnboardAbort(typer.Exit):
    """User chose to abort the wizard."""

    def __init__(self) -> None:
        super().__init__(code=130)


def _config_exists(path: Path) -> bool:
    return path.exists() and path.is_file()


def _ask_overwrite(path: Path) -> bool:
    if not _config_exists(path):
        return True
    choice = questionary.confirm(
        _("onboard.existing_config", path=str(path)),
        default=False,
    ).ask()
    if choice is None:
        raise OnboardAbort()
    return bool(choice)


def _choose_provider() -> str:
    choice = questionary.select(
        _("onboard.provider_select"),
        choices=[
            questionary.Choice("Anthropic", value="anthropic"),
            questionary.Choice("OpenAI", value="openai"),
        ],
    ).ask()
    if choice is None:
        raise OnboardAbort()
    return str(choice)


def _ask_api_key() -> str:
    key = questionary.password(_("onboard.api_key_prompt")).ask()
    if key is None:
        raise OnboardAbort()
    return str(key).strip()


def _ask_workspace() -> Path:
    default = str(data_dir())
    raw = questionary.text(
        _("onboard.workspace_prompt", default=default),
        default=default,
    ).ask()
    if raw is None:
        raise OnboardAbort()
    return Path(os.path.expanduser(raw.strip()))


def _ask_tools() -> dict[str, bool]:
    choices = questionary.checkbox(
        _("onboard.tools_prompt"),
        choices=[
            questionary.Choice("Shell (bash)", value="bash", checked=True),
            questionary.Choice("Web (search/fetch)", value="web", checked=True),
            questionary.Choice("Browser (playwright)", value="browser", checked=False),
        ],
    ).ask()
    if choices is None:
        raise OnboardAbort()
    selected = set(choices)
    return {
        "enable_bash": "bash" in selected,
        "enable_web": "web" in selected,
        "enable_browser": "browser" in selected,
    }


def _write_config(
    path: Path,
    provider: str,
    workspace: Path,
    tools: dict[str, bool],
) -> None:
    cfg: dict[str, Any] = default_config_template()
    cfg["llm"]["default_provider"] = provider

    # default_config_template may not include a tools section.
    if "tools" not in cfg:
        cfg["tools"] = {}

    cfg["tools"]["enable_bash"] = tools.get("enable_bash", True)
    cfg["tools"]["enable_web"] = tools.get("enable_web", True)
    cfg["tools"]["enable_browser"] = tools.get("enable_browser", False)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _smoke_test(provider: str) -> tuple[bool, str]:
    """Quick connectivity check against the provider base URL."""
    import urllib.request

    urls = {
        "anthropic": "https://api.anthropic.com/v1/health",
        "openai": "https://api.openai.com/v1/models",
    }
    url = urls.get(provider, "")
    if not url:
        return True, "unknown provider, skipping connectivity check"
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status < 500:
                return True, ""
            return False, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        # 401/403 means the endpoint is reachable but auth failed —
        # that's expected without a real key in the request.
        if exc.code in (401, 403):
            return True, ""
        return False, f"HTTP {exc.code}"
    except Exception as exc:
        return False, str(exc)


def run_onboard(
    config_path: str = "daemon/config.json",
    skip_smoke: bool = False,
) -> int:
    """Run the interactive onboarding wizard.

    Returns 0 on success, 130 on user abort, 1 on smoke-test failure.
    """
    path = Path(config_path)

    # Step 0: welcome + overwrite guard
    typer.echo(_("onboard.welcome"))
    if not _ask_overwrite(path):
        typer.echo("Skipped — existing config kept.")
        return 0

    # Step 1: provider
    provider = _choose_provider()

    # Step 2: API key (store in secrets.json, not config)
    api_key = _ask_api_key()
    if api_key:
        secret_name = f"llm.{provider}.api_key"
        set_secret(secret_name, api_key)

    # Step 3: workspace
    workspace = _ask_workspace()
    os.environ["XMC_DATA_DIR"] = str(workspace)

    # Step 4: tools
    tools = _ask_tools()

    # Step 5: write config
    _write_config(path, provider, workspace, tools)

    # Step 6: smoke test
    if not skip_smoke:
        typer.echo(_("onboard.smoke_test"))
        ok, err = _smoke_test(provider)
        if ok:
            typer.echo(_("onboard.smoke_ok"))
        else:
            typer.echo(_("onboard.smoke_fail", error=err), err=True)
            return 1

    typer.echo(_("onboard.done"))
    return 0
