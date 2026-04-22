"""``xmclaw doctor`` — diagnose a local setup without running anything.

When ``xmclaw serve`` fails or ``xmclaw chat`` can't connect, the
error the user sees is often four layers removed from the actual cause.
The doctor runs explicit checks in order and prints one line per check
with a clear verdict:

    ✓ config: daemon/config.json parses
    ✓ llm: anthropic provider configured (model=claude-haiku-4-5)
    ✓ tools: 1 allowed dir (/workspace)
    ✓ pairing: token at ~/.xmclaw/v2/pairing_token.txt (64 chars, mode 0600)
    ✓ port 8765: available
    ✓ optional: daemon /health reachable

Every check is a PURE FUNCTION — easy to unit-test without running
a daemon or reaching the network. The CLI layer just calls them in
sequence and prints the results.

The doctor NEVER blocks or hangs. Network checks (port availability,
health probe) use short timeouts; file checks are immediate.
"""
from __future__ import annotations

import json
import socket
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One check's outcome. ``ok=True`` means green; False means red."""

    name: str
    ok: bool
    detail: str
    advisory: str | None = None   # printed in yellow when non-None

    def render(self) -> str:
        # ASCII icons only — the unicode check/cross (✓⚠✗) crash the
        # default Windows-Chinese locale (GBK) when typer.echo tries to
        # encode via sys.stdout. Stay portable.
        icon = "[ok]" if self.ok else ("[!]" if self.advisory else "[x]")
        line = f"  {icon} {self.name}: {self.detail}"
        if self.advisory:
            line += f"\n    -> {self.advisory}"
        return line


# ── individual checks (each takes inputs, returns a CheckResult) ─────────


def check_config_file(path: Path) -> tuple[CheckResult, dict[str, Any] | None]:
    """Read + parse the config. Returns the dict on success so later
    checks can inspect it, or None on failure."""
    if not path.exists():
        return CheckResult(
            name="config",
            ok=False,
            detail=f"not found at {path}",
            advisory=(
                "copy daemon/config.example.json to daemon/config.json "
                "and fill in an LLM api_key"
            ),
        ), None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return CheckResult(
            name="config",
            ok=False,
            detail=f"invalid JSON: {exc}",
            advisory="fix the syntax error and re-run doctor",
        ), None
    if not isinstance(data, dict):
        return CheckResult(
            name="config",
            ok=False,
            detail=f"root must be an object, got {type(data).__name__}",
        ), None
    return CheckResult(
        name="config", ok=True, detail=f"parses ({path})",
    ), data


def check_llm_configured(cfg: dict[str, Any]) -> CheckResult:
    llm_section = cfg.get("llm")
    if not isinstance(llm_section, dict):
        return CheckResult(
            name="llm", ok=False,
            detail="no 'llm' section in config",
            advisory="add an 'llm' section with anthropic or openai credentials",
        )
    for provider in ("anthropic", "openai"):
        p = llm_section.get(provider)
        if not isinstance(p, dict):
            continue
        key = p.get("api_key")
        if not key:
            continue
        model = p.get("default_model") or p.get("model") or "(default)"
        return CheckResult(
            name="llm", ok=True,
            detail=f"{provider} configured (model={model})",
        )
    return CheckResult(
        name="llm", ok=False,
        detail="no provider has api_key set",
        advisory=(
            "add api_key under llm.anthropic or llm.openai. daemon will "
            "fall back to echo mode without one."
        ),
    )


def check_tools_configured(cfg: dict[str, Any]) -> CheckResult:
    tools = cfg.get("tools")
    if tools is None:
        return CheckResult(
            name="tools", ok=True,
            detail="no tools section (LLM-only mode)",
        )
    if not isinstance(tools, dict):
        return CheckResult(
            name="tools", ok=False,
            detail=f"'tools' must be an object, got {type(tools).__name__}",
        )
    allowed = tools.get("allowed_dirs")
    if allowed is None:
        return CheckResult(
            name="tools", ok=False,
            detail="tools section present but allowed_dirs missing",
            advisory="add 'allowed_dirs: [...]' or remove the tools section",
        )
    if not isinstance(allowed, list) or not allowed:
        return CheckResult(
            name="tools", ok=False,
            detail="allowed_dirs is empty or not a list",
            advisory="'allowed_dirs' must be a non-empty list of paths",
        )
    missing = [d for d in allowed if not Path(d).exists()]
    if missing:
        return CheckResult(
            name="tools", ok=True,  # not fatal — dirs can be created later
            detail=f"{len(allowed)} allowed dir(s), {len(missing)} don't exist yet",
            advisory=f"these paths don't exist: {missing}",
        )
    return CheckResult(
        name="tools", ok=True,
        detail=f"{len(allowed)} allowed dir(s)",
    )


def check_pairing_token(path: Path) -> CheckResult:
    """Verify the pairing file is present, readable, and perms-safe on POSIX."""
    if not path.exists():
        return CheckResult(
            name="pairing", ok=True,  # not an error — created on first serve
            detail=f"not yet created (will be created on `xmclaw serve`)",
            advisory=f"expected location: {path}",
        )
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        return CheckResult(
            name="pairing", ok=False,
            detail=f"unreadable: {exc}",
        )
    if not content:
        return CheckResult(
            name="pairing", ok=False,
            detail=f"empty token file at {path}",
            advisory="delete the file and re-run `xmclaw serve` to regenerate",
        )
    # Perms check on POSIX.
    mode_str = ""
    if sys.platform != "win32":
        import os
        mode = os.stat(path).st_mode & 0o777
        mode_str = f", mode {oct(mode)}"
        if mode & 0o077:  # any group/other bit set
            return CheckResult(
                name="pairing", ok=False,
                detail=f"token file has loose perms {oct(mode)}",
                advisory=(
                    f"chmod 600 {path} to restrict to your user only"
                ),
            )
    return CheckResult(
        name="pairing", ok=True,
        detail=f"token at {path} ({len(content)} chars{mode_str})",
    )


def check_port_available(host: str, port: int) -> CheckResult:
    """Can the daemon bind to (host, port)? Sockets are expensive; we do
    a short bind-then-close dance.

    Returns ok=True if the port is FREE (daemon can start fresh), and
    ok=True with a note if it's in use (likely an already-running
    daemon — not an error for ``chat`` but is for ``serve``).
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.settimeout(0.5)
        try:
            s.bind((host, port))
        except OSError:
            return CheckResult(
                name=f"port {port}", ok=True,  # warning, not error
                detail="in use (daemon already running?)",
                advisory=(
                    "if this isn't your daemon, another process is holding "
                    "the port - stop it or pick a different port via `serve --port`"
                ),
            )
        return CheckResult(
            name=f"port {port}", ok=True,
            detail="available",
        )
    finally:
        s.close()


def check_daemon_health(host: str, port: int) -> CheckResult:
    """Optional check: is a daemon actually running and responding?

    We probe ``http://host:port/health`` with a short timeout. Absence
    of a running daemon is NOT an error — the user might be running
    doctor before starting serve. We just report what we found.
    """
    url = f"http://{host}:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=1.0) as resp:  # noqa: S310
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError, OSError):
        return CheckResult(
            name="daemon", ok=True,
            detail=f"not running at {host}:{port} (run `xmclaw serve`)",
        )
    version = body.get("version", "?")
    return CheckResult(
        name="daemon", ok=True,
        detail=f"running at {host}:{port}, version={version}",
    )


# ── runner ──────────────────────────────────────────────────────────────


def run_doctor(
    config_path: Path | str = "daemon/config.json",
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    token_path: Path | None = None,
    probe_daemon: bool = True,
) -> list[CheckResult]:
    """Run every check and return the full list. Callers render to
    stdout however they want.
    """
    from xmclaw.daemon.pairing import default_token_path

    results: list[CheckResult] = []
    cfg_result, cfg = check_config_file(Path(config_path))
    results.append(cfg_result)
    if cfg is not None:
        results.append(check_llm_configured(cfg))
        results.append(check_tools_configured(cfg))
    results.append(check_pairing_token(token_path or default_token_path()))
    results.append(check_port_available(host, port))
    if probe_daemon:
        results.append(check_daemon_health(host, port))
    return results
