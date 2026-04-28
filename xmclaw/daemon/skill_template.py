"""SKILL.md template + inline-shell expansion.

Borrowed from Hermes' agent/skill_commands.py (B-24): a SKILL.md can
include ``${XMC_*}`` template tokens AND ``!`bash command` `` inline
shell snippets. Both are expanded at skill-load time so the body the
agent reads can include fresh, computed data — current date, git
status, file counts, the active workspace's structure, etc.

Without this, every SKILL.md has to be self-contained text. With it,
a skill like "morning project digest" can pull `git status` /
`ls -t | head` / `date` directly into its prompt body.

Security: inline shell is OFF by default (config gate
``evolution.auto_evo.inline_shell.enabled``). Output is bounded to
4 KB and runs with a hard 5-second timeout. Template variables are
always on — they're pure string substitution with no side effects.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Mapping

# ``${XMC_*}`` tokens we recognise. Unknown tokens are left in place
# so a skill author can spot a typo by reading the body.
_TEMPLATE_RE = re.compile(
    r"\$\{(XMC_SKILL_DIR|XMC_SESSION_ID|XMC_WORKSPACE|XMC_TODAY|"
    r"XMC_NOW|XMC_HOME|XMC_PROFILE_DIR)\}"
)

# Single-line inline shell. Greedy stop on backtick OR newline so the
# regex doesn't swallow the rest of the file when an author forgets a
# closing backtick.
_INLINE_SHELL_RE = re.compile(r"!`([^`\n]+)`")

_INLINE_SHELL_DEFAULT_TIMEOUT_S = 5.0
_INLINE_SHELL_MAX_OUTPUT_BYTES = 4096


def substitute_template_vars(
    content: str,
    *,
    skill_dir: Path | None = None,
    session_id: str | None = None,
    workspace: Path | None = None,
    profile_dir: Path | None = None,
    today: str | None = None,
    now: str | None = None,
) -> str:
    """Replace ``${XMC_*}`` tokens with concrete values. Tokens whose
    value is None are left in place (visible in the rendered body so
    the author can see they didn't resolve)."""
    if not content:
        return content

    def _val(key: str) -> str | None:
        if key == "XMC_SKILL_DIR" and skill_dir:
            return str(skill_dir)
        if key == "XMC_SESSION_ID" and session_id:
            return str(session_id)
        if key == "XMC_WORKSPACE" and workspace:
            return str(workspace)
        if key == "XMC_PROFILE_DIR" and profile_dir:
            return str(profile_dir)
        if key == "XMC_HOME":
            return str(Path.home())
        if key == "XMC_TODAY":
            if today is not None:
                return today
            import time
            return time.strftime("%Y-%m-%d")
        if key == "XMC_NOW":
            if now is not None:
                return now
            import time
            return time.strftime("%Y-%m-%d %H:%M:%S")
        return None

    def _replace(m: re.Match) -> str:
        v = _val(m.group(1))
        return v if v is not None else m.group(0)

    return _TEMPLATE_RE.sub(_replace, content)


def expand_inline_shell(
    content: str,
    *,
    cwd: Path | None = None,
    timeout_s: float = _INLINE_SHELL_DEFAULT_TIMEOUT_S,
    max_output_bytes: int = _INLINE_SHELL_MAX_OUTPUT_BYTES,
    env: Mapping[str, str] | None = None,
) -> str:
    """Replace every ``!`cmd` `` snippet with the cmd's stdout.

    Each snippet runs in its own ``bash -c`` (via ``cmd.exe`` shim on
    Windows where bash isn't on PATH). One bad snippet doesn't kill
    the rest — it gets replaced with a ``[inline-shell error: ...]``
    marker and other snippets continue.
    """
    if not content or "!`" not in content:
        return content

    def _run(command: str) -> str:
        try:
            # bash first, fall back to powershell on Windows if bash
            # missing. Most Windows users with XMclaw have git-bash so
            # bash is on PATH; for those who don't we degrade.
            bash_path = _find_bash()
            if bash_path:
                proc = subprocess.run(
                    [bash_path, "-c", command],
                    cwd=str(cwd) if cwd else None,
                    capture_output=True,
                    timeout=max(0.5, float(timeout_s)),
                    check=False,
                    env=dict(env) if env else None,
                )
            elif os.name == "nt":
                proc = subprocess.run(
                    ["cmd", "/c", command],
                    cwd=str(cwd) if cwd else None,
                    capture_output=True,
                    timeout=max(0.5, float(timeout_s)),
                    check=False,
                    env=dict(env) if env else None,
                )
            else:
                return f"[inline-shell error: bash not found]"
        except subprocess.TimeoutExpired:
            return f"[inline-shell timeout after {timeout_s}s: {command[:60]}]"
        except (OSError, FileNotFoundError) as exc:
            return f"[inline-shell error: {exc}]"
        out = proc.stdout or b""
        # Truncate while preserving "lines": keep up to max_output_bytes
        # then add a marker if we cut.
        if len(out) > max_output_bytes:
            out = out[:max_output_bytes] + b"\n[... truncated]"
        return out.decode("utf-8", "replace").rstrip()

    def _replace(m: re.Match) -> str:
        return _run(m.group(1).strip())

    return _INLINE_SHELL_RE.sub(_replace, content)


def _find_bash() -> str | None:
    """Locate bash on PATH. Cached after first call."""
    global _BASH_CACHE
    try:
        return _BASH_CACHE
    except NameError:
        pass
    import shutil
    _BASH_CACHE = shutil.which("bash")
    return _BASH_CACHE
