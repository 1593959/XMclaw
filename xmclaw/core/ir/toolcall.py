"""Tool-Call IR — provider-agnostic representation of a model's tool call.

Anti-requirement #1 (Scheduler must not trust text that *describes* a tool
call): ``ToolCall`` is a structured dataclass. The only way to produce one is
``decode_from_provider`` in a translator, which either returns a valid
``ToolCall`` or returns ``None``. There is no "soft parse" path.

Anti-requirement #3 (per-provider translator fragility): each supported
provider wire format has a translator in ``providers/llm/translators/``,
with double-direction fuzz tests in ``tests/conformance/tool_call_ir.py``.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal


class ToolCallShape(str, Enum):
    ANTHROPIC_NATIVE = "anthropic_native"     # Anthropic tool_use blocks
    OPENAI_TOOL = "openai_tool"                # OpenAI tool_calls array
    OPENAI_JSONMODE = "openai_jsonmode"        # JSON-mode strict schema
    SYNTHETIC = "synthetic"                    # produced internally, no wire format


Provenance = Literal["anthropic", "openai", "json_mode", "synthetic"]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Schema advertised to the model for a single tool."""

    name: str
    description: str
    parameters_schema: dict[str, Any]  # JSON Schema


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A model's request to invoke a tool. Always structured, never text."""

    name: str
    args: dict[str, Any]
    provenance: Provenance
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    raw_snippet: str | None = None  # debug only — the wire bytes it came from
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class ToolResult:
    """A tool's response after invocation.

    ``side_effects`` is the list of paths / URIs the tool has materially
    written to during this invocation. The Honest Grader's
    ``check_side_effect_observable`` verifies each entry is observable
    post-hoc (anti-req #4). Tools with no mutating behavior return
    an empty tuple — the grader treats that as "not applicable" rather
    than "failed to produce a side effect".
    """

    call_id: str
    ok: bool
    content: Any
    error: str | None = None
    latency_ms: float = 0.0
    side_effects: tuple[str, ...] = ()
    schema_version: int = 1
