"""System-prompt assembler — slot-based, mirrors Hermes ``_build_system_prompt``.

Direct port of Hermes ``run_agent.py:4463-4582`` slot ordering, OpenClaw
``buildProjectContextSection`` (``system-prompt.ts:95-125``) for the
"these context files are loaded" framing, and QwenPaw
``build_bootstrap_guidance`` for the first-run prefix.

Slot layout (lower slot = earlier in prompt = harder to drop on
truncation by 3rd-party endpoints with weak system-prompt support):

  0. **DEFAULT_IDENTITY_LINE** — always-on, hardcoded "You are XMclaw…"
  1. **Bootstrap prefix** — when BOOTSTRAP.md is present (rare)
  2. **Persona files** — SOUL/IDENTITY/USER/AGENTS/TOOLS/MEMORY in the
     OpenClaw priority order. Each file is sanitized for prompt-injection
     markers before being concatenated.
  3. **Platform / shell hint** — OS-aware, picked up from the existing
     :func:`xmclaw.daemon.agent_loop._default_system_prompt` shell hint.
  4. **Tools digest** — short summary of the configured tools so the
     model has a recap right next to the user message.

The assembler caches its result by (profile_dir, workspace_dir, mtime
fingerprint). Cache invalidates when any persona file's mtime changes.
"""
from __future__ import annotations

import platform
import time
from pathlib import Path
from typing import Iterable

from xmclaw.core.persona.bootstrap import bootstrap_prefix
from xmclaw.core.persona.loader import (
    PersonaFile,
    load_persona_files,
    sanitize_for_prompt,
)
from xmclaw.core.persona.templates import DEFAULT_IDENTITY_LINE


_SHELL_HINTS: dict[str, str] = {
    "Windows": (
        "The shell is PowerShell. You can use Unix-style aliases (ls, cat, "
        "pwd, rm) OR native Get-ChildItem / Get-Content. Do NOT use bash-isms "
        "like `$(whoami)` or `&&` chaining — PowerShell uses `;` and "
        "`$env:USERNAME`."
    ),
    "Linux": "The shell is bash.",
    "Darwin": "The shell is bash / zsh (macOS).",
}


def _platform_hint() -> str:
    os_name = platform.system()
    home = str(Path.home())
    desktop = str(Path.home() / "Desktop")
    shell = _SHELL_HINTS.get(os_name, "The shell is whatever is on PATH.")
    return (
        f"## 运行时环境\n\n"
        f"OS: {os_name}. User home: `{home}`. Desktop: `{desktop}`.\n"
        f"{shell}"
    )


def _tools_digest(tool_names: Iterable[str] | None) -> str:
    if not tool_names:
        return ""
    names = list(tool_names)
    if not names:
        return ""
    return (
        "## 可用工具\n\n"
        + "可调用：" + ", ".join(f"`{n}`" for n in names) + "。"
    )


def _persona_section(files: list[PersonaFile]) -> str:
    """Render the persona file block — OpenClaw `buildProjectContextSection` shape."""
    if not files:
        return ""
    has_soul = any(f.basename == "SOUL.md" for f in files)
    lines: list[str] = ["## 工作区上下文文件（按优先级载入）", ""]
    if has_soul:
        # OpenClaw `system-prompt.ts:115-118` — explicit instruction so
        # third-party endpoints don't drop persona on long-prompt
        # compression. The next sentence is the literal SOUL-respect line.
        lines.append(
            "If SOUL.md is present, embody its persona and tone. "
            "Avoid stiff, generic replies; follow its guidance unless "
            "higher-priority instructions override it."
        )
        lines.append("")
    for f in files:
        lines.append(f"### {f.basename}")
        lines.append("")
        lines.append(sanitize_for_prompt(f.content).rstrip())
        lines.append("")
    return "\n".join(lines).rstrip()


def _fingerprint(files: list[PersonaFile]) -> tuple:
    """Mtime-based cache key. Built-in templates fingerprint as their content
    hash so edits to ``templates.py`` invalidate caches across reloads."""
    fp: list[tuple] = []
    for f in files:
        if f.layer == "builtin":
            fp.append((f.basename, "builtin", hash(f.content)))
        else:
            try:
                m = f.source.stat().st_mtime_ns
            except OSError:
                m = 0
            fp.append((f.basename, str(f.source), m))
    return tuple(fp)


# Cache by (profile_dir str, workspace_dir str | None, fingerprint, tools tuple).
_CACHE: dict[tuple, str] = {}


def build_system_prompt(
    *,
    profile_dir: Path,
    workspace_dir: Path | None = None,
    tool_names: Iterable[str] | None = None,
    use_cache: bool = True,
) -> str:
    """Assemble the full system prompt for one turn.

    The 5 slots are concatenated with blank lines between them. The
    DEFAULT_IDENTITY_LINE is always slot 0 — defends against the
    underlying model (MiniMax / Qwen / DeepSeek) drifting to its trained
    name when SOUL.md is short or absent.

    Args:
        profile_dir: where the active persona profile lives
            (``~/.xmclaw/persona/profiles/<active>/``).
        workspace_dir: optional project root; ``<root>/.xmclaw/persona/``
            files override the profile copy for matching basenames.
        tool_names: optional iterable of tool names for the digest section.
        use_cache: when False, re-read all files (used by tests).
    """
    files = load_persona_files(
        profile_dir=profile_dir,
        workspace_dir=workspace_dir,
        include_builtin_fallback=True,
    )
    tools_tuple = tuple(tool_names or ())
    cache_key = (
        str(profile_dir),
        str(workspace_dir) if workspace_dir else None,
        _fingerprint(files),
        tools_tuple,
    )
    if use_cache and cache_key in _CACHE:
        return _CACHE[cache_key]

    parts: list[str] = []

    # Slot 0: hard identity line, always.
    parts.append(DEFAULT_IDENTITY_LINE)

    # Slot 1: bootstrap prefix when applicable.
    boot = bootstrap_prefix(
        profile_dir=profile_dir, workspace_dir=workspace_dir
    )
    if boot:
        parts.append(boot)

    # Slot 2: persona files.
    persona = _persona_section(files)
    if persona:
        parts.append(persona)

    # Slot 3: platform hint.
    parts.append(_platform_hint())

    # Slot 4: tools digest.
    digest = _tools_digest(tool_names)
    if digest:
        parts.append(digest)

    out = "\n\n".join(p for p in parts if p)
    _CACHE[cache_key] = out
    return out


def clear_cache() -> None:
    """Drop the assembled-prompt cache. Mainly for tests + ``/reload``."""
    _CACHE.clear()
