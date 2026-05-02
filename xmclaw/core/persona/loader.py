"""Persona file loader — read 7-file SOUL pack with project overlay.

Direct port of OpenClaw's ``src/agents/system-prompt.ts:44-93`` priority
ordering and ``src/agents/workspace.ts:19-86`` cache-by-mtime pattern,
specialized for Python's pathlib.

Resolution layers (highest precedence first per basename):

    1. ``<workspace>/.xmclaw/persona/<basename>``     ← project-level overlay
    2. ``~/.xmclaw/persona/profiles/<active>/<basename>`` ← user profile
    3. Built-in template under :mod:`xmclaw.core.persona.templates`

We intentionally do NOT merge file contents from multiple layers — the
highest-precedence layer wins for a given basename, mirroring how
OpenClaw / Hermes treat workspace-overlay vs. global rules.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from xmclaw.core.persona import templates as _templates

# Same priority as OpenClaw `system-prompt.ts:44-52`. Lower number = earlier
# in the assembled prompt. ``bootstrap.md`` sits between tools.md and
# memory.md so the bootstrap dialogue runs after the agent has read who it
# is and which tools are available.
CONTEXT_FILE_ORDER: dict[str, int] = {
    "agents.md": 10,
    "soul.md": 20,
    "identity.md": 30,
    "learning.md": 35,        # B-197 Phase 4: 本能/进化教材
    "user.md": 40,
    "tools.md": 50,
    "bootstrap.md": 60,
    "memory.md": 70,
}

# Canonical-cased filenames we hand back to callers (filesystem may be
# case-insensitive on Windows but the prompt looks better with the case
# users typically write).
PERSONA_BASENAMES: tuple[str, ...] = (
    "AGENTS.md",
    "SOUL.md",
    "IDENTITY.md",
    "LEARNING.md",            # B-197 Phase 4
    "USER.md",
    "TOOLS.md",
    "BOOTSTRAP.md",
    "MEMORY.md",
)


@dataclass(frozen=True, slots=True)
class PersonaFile:
    """One loaded persona file. ``layer`` records which layer won."""

    basename: str  # canonical-cased ("SOUL.md")
    content: str
    source: Path
    layer: str  # "project" | "profile" | "builtin"

    @property
    def order(self) -> int:
        return CONTEXT_FILE_ORDER.get(self.basename.lower(), 999)


def _read_text(p: Path) -> str | None:
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return _strip_yaml_frontmatter(text)


def _strip_yaml_frontmatter(text: str) -> str:
    """Strip a leading ``---\\n...\\n---\\n`` block.

    OpenClaw / Hermes / QwenPaw all do this — frontmatter is for the loader,
    not for the LLM. Mirrors hermes ``prompt_builder.py:113-127``.
    """
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---\n", 4)
    if end == -1:
        return text
    return text[end + 5 :].lstrip("\n")


def _candidate_paths(
    basename: str,
    *,
    profile_dir: Path,
    workspace_dir: Path | None,
) -> list[tuple[Path, str]]:
    """Return [(path, layer), ...] in precedence order (highest first)."""
    candidates: list[tuple[Path, str]] = []
    if workspace_dir is not None:
        candidates.append(
            (workspace_dir / ".xmclaw" / "persona" / basename, "project")
        )
        # Case-insensitive Windows can resolve fine, but Unix users may
        # uppercase or lowercase — try both.
        candidates.append(
            (workspace_dir / ".xmclaw" / "persona" / basename.lower(), "project")
        )
    candidates.append((profile_dir / basename, "profile"))
    candidates.append((profile_dir / basename.lower(), "profile"))
    return candidates


def load_persona_files(
    *,
    profile_dir: Path,
    workspace_dir: Path | None = None,
    include_builtin_fallback: bool = True,
) -> list[PersonaFile]:
    """Load all persona files in priority order.

    Args:
        profile_dir: ``~/.xmclaw/persona/profiles/<active>/`` typically.
        workspace_dir: optional project root. If given, files under
            ``<root>/.xmclaw/persona/`` override the profile copy.
        include_builtin_fallback: when True, missing files fall back to
            the bundled :mod:`xmclaw.core.persona.templates`. When False,
            missing files are simply omitted (used by tests that want to
            verify the no-template state).

    Returns:
        ``PersonaFile`` list, sorted by :data:`CONTEXT_FILE_ORDER`. The
        list is empty if no files were found and ``include_builtin_fallback``
        is False.
    """
    out: list[PersonaFile] = []
    for canonical in PERSONA_BASENAMES:
        # BOOTSTRAP.md is special — only inject if it actually exists on
        # disk (presence is the trigger for bootstrap mode). Never
        # synthesize from the template into the prompt.
        if canonical == "BOOTSTRAP.md":
            for p, layer in _candidate_paths(
                canonical, profile_dir=profile_dir, workspace_dir=workspace_dir
            ):
                if p.is_file():
                    text = _read_text(p)
                    if text is not None:
                        out.append(PersonaFile(canonical, text, p, layer))
                        break
            continue

        # Normal file: scan layers, fall back to built-in template last.
        loaded: PersonaFile | None = None
        for p, layer in _candidate_paths(
            canonical, profile_dir=profile_dir, workspace_dir=workspace_dir
        ):
            if p.is_file():
                text = _read_text(p)
                if text is not None and text.strip():
                    loaded = PersonaFile(canonical, text, p, layer)
                    break
        if loaded is None and include_builtin_fallback:
            tmpl = _templates.TEMPLATES.get(canonical)
            if tmpl is not None and tmpl.strip():
                loaded = PersonaFile(
                    canonical,
                    tmpl,
                    Path(f"<builtin:{canonical}>"),
                    "builtin",
                )
        if loaded is not None:
            out.append(loaded)

    out.sort(key=lambda f: f.order)
    return out


def ensure_default_profile(profile_dir: Path) -> list[Path]:
    """Materialize built-in templates into ``profile_dir`` on first install.

    Idempotent: existing files are not overwritten. Returns the list of
    paths that were actually written. Skips ``BOOTSTRAP.md`` — that one
    is opt-in (``ensure_bootstrap_marker`` writes it when the user wants
    the first-run interview).
    """
    profile_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for basename, content in _templates.TEMPLATES.items():
        if basename == "BOOTSTRAP.md":
            continue  # opt-in
        target = profile_dir / basename
        if target.exists():
            continue
        try:
            target.write_text(content, encoding="utf-8")
            written.append(target)
        except OSError:
            continue
    return written


def ensure_bootstrap_marker(profile_dir: Path) -> Path | None:
    """Write the BOOTSTRAP.md template if it doesn't already exist.

    Called explicitly when the user wants the first-run interview pattern
    (or when XMclaw detects it's the first install and the profile dir is
    fresh). The agent reads BOOTSTRAP.md, runs the interview, writes
    IDENTITY.md / USER.md, then deletes BOOTSTRAP.md — same flow as
    OpenClaw and QwenPaw.
    """
    target = profile_dir / "BOOTSTRAP.md"
    if target.exists():
        return None
    try:
        target.write_text(_templates.BOOTSTRAP_TEMPLATE, encoding="utf-8")
        return target
    except OSError:
        return None


def has_bootstrap_pending(
    *, profile_dir: Path, workspace_dir: Path | None
) -> bool:
    """Return True iff a BOOTSTRAP.md file exists in any layer."""
    for p, _layer in _candidate_paths(
        "BOOTSTRAP.md", profile_dir=profile_dir, workspace_dir=workspace_dir
    ):
        if p.is_file():
            try:
                if p.stat().st_size > 0:
                    return True
            except OSError:
                continue
    return False


# Heuristic for prompt-injection: filter known threat tokens from
# user-edited persona files before they land in the system prompt.
# Mirrors hermes `prompt_builder.py:36-71` — the user is welcome to write
# anything in SOUL.md but we don't want a malicious external skill that
# wrote into MEMORY.md to redefine the agent.
_CONTEXT_THREAT_PATTERNS: tuple[str, ...] = (
    "ignore previous instructions",
    "disregard your instructions",
    "ignore your system prompt",
    "you are no longer xmclaw",
    "from now on you are",
    "<|system|>",
    "<|im_start|>",
    "</system>",
)

_INVISIBLE_CHARS = "​‌‍﻿‮‭"


def sanitize_for_prompt(text: str) -> str:
    """Strip prompt-injection markers from user-edited context.

    Two layers (B-79):

    1. The legacy 8-pattern English blacklist + zero-width-char strip
       (this module's ``_CONTEXT_THREAT_PATTERNS`` + ``_INVISIBLE_CHARS``).
       Cheap, runs first.
    2. The full :mod:`xmclaw.security.prompt_scanner` — 70+ patterns
       covering Chinese phrasing, jailbreaks, indirect injection, tool
       hijack, and ``<|system|>``-style role markers. Used to be wired
       only on the SOURCE_TOOL_RESULT / SOURCE_MEMORY_RECALL paths in
       agent_loop; persona files (which are equally untrusted when
       restored from a backup, pulled from a tampered branch, or
       cross-written by another agent) had nothing.

    Findings at HIGH or above are redacted in place via
    :func:`xmclaw.security.prompt_scanner.redact` — they leave a
    ``[redacted:<pattern_id>]`` placeholder, surfacing to the user
    that something looked off rather than silently swallowing it.
    LOW / MEDIUM hits pass through (a SOUL.md line discussing the
    *concept* of prompt injection should not break the prompt).
    """
    out = text
    for ch in _INVISIBLE_CHARS:
        out = out.replace(ch, "")
    lower = out.lower()
    for marker in _CONTEXT_THREAT_PATTERNS:
        if marker in lower:
            # Replace the line containing it with a notice. Keeps the rest
            # of the file usable.
            new_lines = []
            for line in out.split("\n"):
                if marker in line.lower():
                    new_lines.append(
                        "[XMclaw: line removed — looked like a prompt-injection marker]"
                    )
                else:
                    new_lines.append(line)
            out = "\n".join(new_lines)
            lower = out.lower()

    # B-79: defer-import to avoid a hard core->security edge at module
    # load time (security is a peer subpackage, not a dependency of
    # core). The import is cheap; this function is only called during
    # persona assembly, not on every turn.
    try:
        from xmclaw.security.prompt_scanner import (
            Severity, redact, scan_text,
        )
    except Exception:  # noqa: BLE001 — security pkg load failure must
        # not break the agent's ability to read its own SOUL.md.
        return out
    try:
        result = scan_text(out, severity_threshold=Severity.HIGH)
    except Exception:  # noqa: BLE001 — same reason as above.
        return out
    if not result.any_findings:
        return out
    return redact(out, result)
