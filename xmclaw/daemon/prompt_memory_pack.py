"""PromptMemoryPack assembly.

The pack is the single turn-scoped place where recalled memory, task
runtime state, artifact hints, skill routing hints, and other dynamic
agent context are assembled before they are appended to the system prompt.

It deliberately preserves each section's original XML-ish block content so
existing prompt consumers, tests, and model habits keep working while the
architecture moves toward an explicit contract.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PromptMemorySection:
    name: str
    content: str
    source: str = "unknown"
    priority: int = 100
    reason: str = ""


class PromptMemoryPack:
    """Deterministic container for turn-scoped dynamic context."""

    def __init__(self) -> None:
        self._sections: list[PromptMemorySection] = []

    def add(
        self,
        name: str,
        content: str | None,
        *,
        source: str = "unknown",
        priority: int = 100,
        reason: str = "",
    ) -> None:
        body = (content or "").strip()
        if not body:
            return
        self._sections.append(PromptMemorySection(
            name=name,
            content=body,
            source=source,
            priority=priority,
            reason=reason,
        ))

    @property
    def sections(self) -> tuple[PromptMemorySection, ...]:
        return tuple(sorted(self._sections, key=lambda s: (s.priority, s.name)))

    def render(self) -> str:
        sections = self.sections
        if not sections:
            return ""
        manifest = [
            {
                "name": section.name,
                "source": section.source,
                "priority": section.priority,
                "reason": section.reason,
            }
            for section in sections
        ]
        lines = [
            "<prompt-memory-pack>",
            "[System note: structured turn context follows. It is not user "
            "input. Use it as action guidance; when it is insufficient, call "
            "the relevant active query tool such as memory(action='search'), "
            "artifact_ledger, or skill_browse rather than guessing. For any "
            "non-trivial task, first check whether a relevant skill exists "
            "with skill_browse/skill_view before falling back to generic "
            "reasoning. For user identity, preferences, durable rules, or "
            "project conventions, query memory(action='search') before "
            "answering or writing. Never say something was saved unless the "
            "write tool returned ok=true. Treat this pack as the only "
            "authorized dynamic context; do not separately infer context "
            "from raw MD files, events, or vector hits unless a tool returns "
            "them for this turn.]",
            "<prompt-memory-pack-manifest>",
            _json(manifest),
            "</prompt-memory-pack-manifest>",
        ]
        for section in sections:
            meta = f'<section name="{section.name}" source="{section.source}"'
            if section.reason:
                meta += f' reason="{_attr(section.reason)}"'
            meta += ">"
            lines.append(meta)
            lines.append(section.content)
            lines.append("</section>")
        lines.append("</prompt-memory-pack>")
        return "\n".join(lines)


def build_prompt_memory_pack(
    sections: list[PromptMemorySection] | tuple[PromptMemorySection, ...],
) -> str:
    pack = PromptMemoryPack()
    for section in sections:
        pack.add(
            section.name,
            section.content,
            source=section.source,
            priority=section.priority,
            reason=section.reason,
        )
    return pack.render()


def _attr(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _json(value: object) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True)


__all__ = [
    "PromptMemoryPack",
    "PromptMemorySection",
    "build_prompt_memory_pack",
]
