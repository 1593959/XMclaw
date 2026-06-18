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


def _smoke_test(provider: str, api_key: str | None = None) -> tuple[bool, str]:
    """Real key-validation smoke test.

    B-347 (Sprint 1): pre-B-347 this was a HEAD request without the
    api_key in headers, treating 401/403 as "OK, endpoint reachable".
    That meant onboard ALWAYS passed even when the key was invalid /
    expired — user finished onboarding, daemon dropped to echo mode,
    user typed "hello" and got "hello" back, no idea why.

    Now: actually call a 1-token endpoint with the key in headers.
    200/400 (key valid, payload may be rejected) = OK. 401 = bad key.
    Network failure / 5xx = endpoint unreachable.
    """
    import json as _json
    import urllib.request
    import urllib.error

    if not api_key:
        # No key provided — still skip (user may want to set it later
        # via env var). But warn instead of declaring success.
        return True, "no api_key provided — skipping (set XMC__llm.* env vars later or rerun with key)"

    if provider == "anthropic":
        url = "https://api.anthropic.com/v1/messages"
        body = _json.dumps({
            "model": "claude-haiku-4-5",
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "ping"}],
        }).encode("utf-8")
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
    elif provider == "openai":
        url = "https://api.openai.com/v1/models"
        body = None
        headers = {
            "Authorization": f"Bearer {api_key}",
        }
    else:
        return True, f"unknown provider {provider!r}, skipping connectivity check"

    req = urllib.request.Request(
        url,
        data=body,
        headers=headers,
        method="POST" if body else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            if resp.status == 200:
                return True, ""
            return False, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        # 401 = bad key (the user's actual problem). 403 = key valid
        # but lacks permission for THIS endpoint — still treat as OK
        # for onboard, the user might be on a tier that can't list
        # models but can still complete (anthropic has this shape).
        # 400 = payload rejected but auth passed (anthropic returns
        # this for missing model on free tier, etc).
        if exc.code == 401:
            try:
                body_text = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:  # noqa: BLE001
                body_text = ""
            return False, (
                f"HTTP 401 — API key 被拒。请确认你贴的是当前有效的 key，"
                f"且对应的 provider 选对了。详情: {body_text}"
            )
        if exc.code in (403, 400):
            return True, f"HTTP {exc.code} — auth passed (endpoint-specific rejection)"
        return False, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001
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
        # B-425: reject test/placeholder keys so they never pollute the
        # secrets layer and synthesise a phantom legacy "default" profile.
        from xmclaw.daemon.factory import _is_placeholder_api_key
        if _is_placeholder_api_key(api_key):
            typer.echo(
                "  [!]  API key 看起来是测试/占位值，跳过写入。"
                "请提供真实 key 或留空稍后通过 'xmclaw config set-secret' 设置。",
                err=True,
            )
        else:
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
    # B-347: now validates the API key by ACTUALLY hitting the
    # provider with the key in headers. 401 → fail-fast onboard;
    # user gets to fix the key NOW instead of discovering at first
    # message that daemon dropped to echo mode silently.
    if not skip_smoke:
        typer.echo(_("onboard.smoke_test"))
        ok, err = _smoke_test(provider, api_key=api_key)
        if ok:
            typer.echo(_("onboard.smoke_ok"))
        else:
            typer.echo(_("onboard.smoke_fail", error=err), err=True)
            return 1

    typer.echo(_("onboard.done"))
    return 0
