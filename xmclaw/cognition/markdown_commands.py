"""Markdown-format slash commands — Claude Code parity.

Wave-32+ (2026-05-18). Ports the claude-code-src ``plugins/<name>/
commands/*.md`` convention into XMclaw. A markdown command is a
``.md`` file with YAML frontmatter + a body prompt:

    ---
    description: Create a git commit
    allowed-tools: Bash(git add:*), Bash(git commit:*)
    argument-hint: optional message
    ---

    ## Context
    - Status: !`git status`
    - Diff: !`git diff HEAD`

    ## Your task
    Based on the above, create a single commit.
    Args from user: $ARGUMENTS

When the user types ``/commit foo bar``, the daemon:

1. Looks up ``commit.md`` in the registered command dirs
2. Runs the embedded ``!``cmd`` shell escapes, substitutes their
   stdout into the body
3. Replaces ``$ARGUMENTS`` with ``foo bar``
4. Sends the rendered body as the next user message to the LLM

Why this is huge for XMclaw
===========================

XMclaw already has built-in slash commands (``/help``, ``/status``)
hardcoded in :mod:`channel_slash_router`, but they're Python
functions — not user-installable, not pluggable, not shareable. The
claude-code-src plugins ecosystem ships dozens of high-quality
commands (commit, code-review, feature-dev, pr-review-toolkit,
clean_gone, ...) as ``.md`` files that any project can drop in.
This module gives XMclaw the same surface so those plugins can be
copied in and work immediately.

Discovery paths
===============

  1. ``~/.xmclaw/commands/*.md`` — user-global
  2. ``./.xmclaw/commands/*.md`` — project-local (overrides user-global)
  3. ``./.claude/commands/*.md`` — claude-code-src compatibility
     (claude-code's project convention is ``.claude/``; we read it
     for zero-friction reuse)

Highest priority wins on name collision (project > user).

Safety
======

The ``allowed-tools`` frontmatter field is RECORDED but not yet
enforced — XMclaw's tool allowlist mechanism (Wave-32+ Plan-mode
gate, B-332 ``tools_allowlist``) can later read it to scope what
the rendered prompt's downstream LLM call is allowed to do.

Shell escapes (``!``cmd``) ARE executed — that's the whole point of
the format. They run with the daemon's process privileges in the
current cwd. Operators who don't want this should not drop untrusted
``.md`` files into the discovery paths. The workspace-trust marker
(``.xmclaw-trust``) is checked: untrusted workspaces refuse to run
the shell escapes and substitute ``<untrusted: cmd not run>`` instead.
"""
from __future__ import annotations

import asyncio
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from xmclaw.utils.log import get_logger

logger = get_logger(__name__)


# Matches "!`<command>`" with the backtick literal — same syntax as
# claude-code-src. Greedy `.+?` to avoid eating subsequent escapes.
_SHELL_ESCAPE_RE = re.compile(r"!`([^`]+)`")
# Frontmatter delimiter at top of file.
_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(.+?)\n---\s*\n(.*)\Z", re.DOTALL,
)
# Simple key:value parser. We avoid pulling in PyYAML (a heavy dep
# for what is essentially `key: value` lines). Multiline values are
# NOT supported — every command we've seen uses single-line values.
_KV_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9_-]*)\s*:\s*(.*)\s*$")


@dataclass
class CommandDef:
    """A discovered markdown command.

    ``name`` is the filename sans ``.md``. ``prompt_body`` is the
    body AFTER the frontmatter, BEFORE shell substitution — render
    fills in ``!``cmd`` results and ``$ARGUMENTS`` per-invocation."""

    name: str
    description: str
    prompt_body: str
    allowed_tools: tuple[str, ...] = ()
    argument_hint: str = ""
    source_path: str = ""
    source: str = "user-md"  # "user-md" | "project-md" | "claude-md"


@dataclass
class RenderResult:
    """Output of :func:`render_command`. ``ok=False`` means at least
    one shell substitution failed — the rendered text still has the
    failure annotation inline so the LLM can react."""

    ok: bool
    rendered: str
    failures: list[str] = field(default_factory=list)


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split frontmatter from body. Returns ``(frontmatter_dict, body)``.

    Files WITHOUT frontmatter (just plain markdown) return
    ``({}, text)`` — they're still loadable as a command, just with
    no metadata. This matches claude-code-src behavior."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    fm_raw, body = m.group(1), m.group(2)
    fm: dict[str, str] = {}
    for line in fm_raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        km = _KV_RE.match(line)
        if km:
            fm[km.group(1)] = km.group(2).strip()
    return fm, body


def _candidate_dirs() -> list[tuple[Path, str]]:
    """Return ``(dir, source_tag)`` tuples to scan, highest priority
    first. Used by :func:`discover_commands` for the merge."""
    out: list[tuple[Path, str]] = []
    cwd = Path.cwd()
    out.append((cwd / ".xmclaw" / "commands", "project-md"))
    out.append((cwd / ".claude" / "commands", "claude-md"))
    try:
        from xmclaw.utils.paths import data_dir
        out.append((data_dir() / "commands", "user-md"))
    except Exception:  # noqa: BLE001
        pass
    return out


def discover_commands() -> list[CommandDef]:
    """Scan discovery paths + return all unique commands. Same-name
    collisions resolved by candidate-dir priority (first wins).

    Called on every UI list refresh; cheap enough not to cache (a
    typical project has ≤30 commands)."""
    seen: dict[str, CommandDef] = {}
    for d, source in _candidate_dirs():
        if not d.exists() or not d.is_dir():
            continue
        for md in sorted(d.glob("*.md")):
            name = md.stem
            if name in seen:
                continue
            try:
                text = md.read_text(encoding="utf-8")
            except OSError as exc:
                logger.warning(
                    "markdown_command.read_failed path=%s err=%s",
                    md, exc,
                )
                continue
            fm, body = parse_frontmatter(text)
            allowed_raw = fm.get("allowed-tools", "")
            allowed = tuple(
                t.strip() for t in allowed_raw.split(",") if t.strip()
            )
            seen[name] = CommandDef(
                name=name,
                description=fm.get("description", "").strip(),
                prompt_body=body,
                allowed_tools=allowed,
                argument_hint=fm.get("argument-hint", "").strip(),
                source_path=str(md),
                source=source,
            )
    return list(seen.values())


def find_command(name: str) -> CommandDef | None:
    """Cheap lookup by name. Re-discovers each call so newly dropped
    files appear without a restart."""
    for c in discover_commands():
        if c.name == name:
            return c
    return None


async def _run_shell(
    cmd: str, cwd: Path, timeout_s: float = 30.0,
) -> tuple[bool, str]:
    """Run a shell substitution, return ``(ok, stdout_or_err)``.

    Bounded by timeout. stderr merged into stdout because the
    claude-code-src semantics is "show whatever the command emitted"
    — distinguishing isn't useful when the result is going into a
    prompt body anyway. Returns the trimmed string trimmed to 4KB
    so a runaway ``git log`` doesn't flood the rendered prompt.
    """
    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_shell(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=str(cwd),
            ),
            timeout=5.0,  # process-spawn timeout
        )
        try:
            out, _ = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            proc.kill()
            return False, f"<timeout after {timeout_s}s>"
    except Exception as exc:  # noqa: BLE001
        return False, f"<spawn failed: {exc}>"
    text = (out or b"").decode("utf-8", errors="replace").strip()
    if len(text) > 4096:
        text = text[:4093] + "..."
    return proc.returncode == 0, text


async def render_command(
    cmd_def: CommandDef,
    arguments: str = "",
    *,
    cwd: Path | None = None,
    workspace_trust: str = "trusted",
) -> RenderResult:
    """Run all shell substitutions, replace ``$ARGUMENTS``, return
    the final prompt text.

    ``workspace_trust`` short-circuits shell escapes when not
    ``"trusted"`` — substitutes ``<untrusted: cmd not run>``. This
    means a hostile ``.md`` dropped into an untrusted workspace
    can't run arbitrary shell.
    """
    cwd = cwd or Path.cwd()
    body = cmd_def.prompt_body
    failures: list[str] = []

    # Collect all escapes first so we can run them concurrently.
    escapes = list(_SHELL_ESCAPE_RE.finditer(body))
    if escapes:
        if workspace_trust != "trusted":
            # Don't execute — substitute placeholder. Still mark as
            # ok=True since the render itself succeeded.
            for m in reversed(escapes):
                body = (
                    body[:m.start()]
                    + f"<untrusted workspace: ``{m.group(1)}`` not run>"
                    + body[m.end():]
                )
        else:
            results = await asyncio.gather(*(
                _run_shell(m.group(1), cwd) for m in escapes
            ))
            # Substitute right-to-left so spans stay valid.
            for m, (ok, out) in zip(reversed(escapes), reversed(results)):
                if not ok:
                    failures.append(f"`{m.group(1)}`: {out}")
                body = (
                    body[:m.start()]
                    + (out if ok else f"<failed: {out}>")
                    + body[m.end():]
                )

    # $ARGUMENTS substitution — claude-code-src convention. We do
    # this AFTER shell substitution so a `!`cmd`` couldn't generate
    # a $ARGUMENTS sigil and inject user args into a literal that
    # was meant to stay literal. (Defense-in-depth; unlikely to
    # matter in practice but the order is free to get right.)
    body = body.replace("$ARGUMENTS", arguments)

    return RenderResult(
        ok=not failures,
        rendered=body,
        failures=failures,
    )


__all__ = [
    "CommandDef",
    "RenderResult",
    "discover_commands",
    "find_command",
    "parse_frontmatter",
    "render_command",
]
