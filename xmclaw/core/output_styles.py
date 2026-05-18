"""OutputStyles — preset prompt fragments that change the agent's tone
without changing its tools or task.

Wave-32+ (2026-05-18). Ports the free-code-main ``outputStyles/``
pattern. Two built-in styles ship out of the box:

  * **Explanatory** — agent adds short educational "Insight" boxes
    around code changes so the user understands *why*.
  * **Learning** — agent inserts ``TODO(human)`` stubs and explicitly
    asks the user to fill them in, optimizing for hands-on practice.

The style is **session-scoped state**, like plan mode: kept in a
process-level dict keyed by session_id, set by the WS handler (from
the frontend Style chip / config) or by the agent via the
:tool:`set_output_style` LLM tool. AgentLoop reads it at system-
prompt build time and splices the selected style's prompt into the
system content between cache breakpoints.

Custom styles
=============

Operators can drop ``~/.xmclaw/output_styles/<name>.md`` files (or
``./xmclaw/output_styles/<name>.md`` per-project). Each file's
contents become the style's prompt; the filename (sans ``.md``)
becomes the style name. Loaded lazily on first lookup; cached
process-wide. No restart required — if a registered name is missing
from disk we fall back to built-in.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final


@dataclass(frozen=True, slots=True)
class OutputStyle:
    name: str
    description: str
    prompt: str
    source: str  # "built-in" | "user-md" | "project-md"


DEFAULT_OUTPUT_STYLE: Final[str] = "default"


_EXPLANATORY_INSIGHTS_FRAGMENT = """\
## Insights
In order to encourage learning, before and after writing code, always
provide brief educational explanations about implementation choices
using:

    ★ Insight ─────────────────────────────────────
    [2-3 key educational points]
    ─────────────────────────────────────────────────

These insights are PART OF THE CONVERSATION, not the codebase. Focus
on points specific to THIS codebase and code you just touched —
not general programming concepts the user already knows.
"""


_BUILTIN_STYLES: dict[str, OutputStyle] = {
    DEFAULT_OUTPUT_STYLE: OutputStyle(
        name="default",
        description="No additional style — agent behaves per its base prompt.",
        prompt="",
        source="built-in",
    ),
    "Explanatory": OutputStyle(
        name="Explanatory",
        description="Agent explains its implementation choices and codebase patterns inline.",
        prompt=(
            "You are running in EXPLANATORY output style. In addition "
            "to completing the user's software engineering task, "
            "you should provide educational insights about the "
            "codebase along the way. Be clear and educational, "
            "providing helpful explanations while remaining focused "
            "on the task. Balance educational content with task "
            "completion. When providing insights, you may exceed "
            "typical length constraints, but remain focused and "
            "relevant.\n\n"
            "# Explanatory Style Active\n"
            f"{_EXPLANATORY_INSIGHTS_FRAGMENT}"
        ),
        source="built-in",
    ),
    "Learning": OutputStyle(
        name="Learning",
        description=(
            "Agent inserts TODO(human) stubs and asks the user to "
            "write 2-10 line code pieces for hands-on practice."
        ),
        prompt=(
            "You are running in LEARNING output style. In addition to "
            "completing software-engineering tasks, you help the user "
            "learn through hands-on practice and educational "
            "insights. Be collaborative and encouraging. Balance "
            "task completion with learning by requesting user input "
            "for meaningful design decisions while handling routine "
            "implementation yourself.\n\n"
            "# Learning Style Active\n\n"
            "## Requesting Human Contributions\n"
            "When generating 20+ lines of code that involve design "
            "decisions, business logic with multiple valid approaches, "
            "or key algorithms — ask the user to write 2-10 lines of "
            "that code themselves. Frame it as a `TODO(human)` stub "
            "in the file + a 'Learn by Doing' prompt in chat:\n\n"
            "  • **Learn by Doing**\n"
            "  • **Context:** what's built and why this decision matters\n"
            "  • **Your Task:** specific function/section, mention "
            "    file and TODO(human) — do not include line numbers\n"
            "  • **Guidance:** trade-offs / constraints to consider\n\n"
            "Rules: ONE TODO(human) at a time. Add the stub before "
            "asking. After asking, STOP — wait for the user's code "
            "before continuing. Do not implement the stub yourself.\n\n"
            f"{_EXPLANATORY_INSIGHTS_FRAGMENT}"
        ),
        source="built-in",
    ),
}


# Per-session style selection. Process-level — like the plan-mode set,
# session ids are globally unique within the daemon so we don't need
# per-AgentLoop scoping.
_SESSION_STYLES: dict[str, str] = {}


def list_styles() -> list[OutputStyle]:
    """Return all known styles (built-in + on-disk). Cached per-call
    is fine — the disk scan is cheap and the call site is the
    settings UI, not the hot path."""
    out: dict[str, OutputStyle] = dict(_BUILTIN_STYLES)
    for style in _load_disk_styles():
        out[style.name] = style  # disk overrides built-in same-name
    return list(out.values())


def get_style(name: str | None) -> OutputStyle:
    """Resolve a style name → :class:`OutputStyle`. Unknown names
    fall back to ``default`` rather than erroring — same lenient
    posture as plan_mode's "exit-when-not-in is a no-op."
    """
    if not name:
        return _BUILTIN_STYLES[DEFAULT_OUTPUT_STYLE]
    # Check disk first so user overrides win over built-ins.
    for s in _load_disk_styles():
        if s.name == name:
            return s
    return _BUILTIN_STYLES.get(name) or _BUILTIN_STYLES[DEFAULT_OUTPUT_STYLE]


def session_style(session_id: str) -> OutputStyle:
    """Style currently active for ``session_id``. Returns default
    when nothing is set."""
    return get_style(_SESSION_STYLES.get(session_id))


def set_session_style(session_id: str, name: str | None) -> None:
    """Idempotent setter. ``None`` / ``"default"`` clears the entry
    so :func:`session_style` returns the default style. Used by the
    WS handler and the ``set_output_style`` LLM tool."""
    if not name or name == DEFAULT_OUTPUT_STYLE:
        _SESSION_STYLES.pop(session_id, None)
        return
    _SESSION_STYLES[session_id] = name


def clear_all_session_styles() -> None:
    """Test helper — wipe per-session selections between cases."""
    _SESSION_STYLES.clear()


def _candidate_disk_dirs() -> list[Path]:
    """Where to look for user / project output-style markdown files.

    Priority: project (``./xmclaw/output_styles/``) > user
    (``~/.xmclaw/output_styles/``). Matches the free-code precedence
    rule. Project dir is resolved against the current working
    directory; running outside a project just means it's empty.
    """
    out: list[Path] = []
    try:
        from xmclaw.utils.paths import data_dir
        out.append(data_dir() / "output_styles")
    except Exception:  # noqa: BLE001
        pass
    out.append(Path.cwd() / "xmclaw" / "output_styles")
    return out


def _load_disk_styles() -> list[OutputStyle]:
    """Scan candidate dirs for ``*.md`` style files. Each file's
    base name (sans ``.md``) becomes the style name; its contents
    become the prompt. Cheap enough to scan per-call — no caching."""
    out: list[OutputStyle] = []
    seen: set[str] = set()
    for d in _candidate_disk_dirs():
        if not d.exists() or not d.is_dir():
            continue
        for md in sorted(d.glob("*.md")):
            name = md.stem
            if name in seen:
                continue  # earlier dir already won
            try:
                text = md.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if not text:
                continue
            seen.add(name)
            out.append(OutputStyle(
                name=name,
                description=f"User-defined output style ({md.name})",
                prompt=text,
                source="user-md" if "v2" in str(d) else "project-md",
            ))
    return out


__all__ = [
    "DEFAULT_OUTPUT_STYLE",
    "OutputStyle",
    "clear_all_session_styles",
    "get_style",
    "list_styles",
    "session_style",
    "set_session_style",
]
