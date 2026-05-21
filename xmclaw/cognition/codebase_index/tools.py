"""CodebaseIndex ToolProvider — expose ``codebase_search``, ``codebase_ask``,
``codebase_conventions`` to the agent loop.

These tools let the agent query the local codebase index without needing
to know sqlite-vec or embedding details.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from xmclaw.core.ir import ToolCall, ToolResult, ToolSpec
from xmclaw.cognition.codebase_index.conventions import ProjectConventions, extract_conventions, render_conventions
from xmclaw.cognition.codebase_index.store import CodebaseStore
from xmclaw.providers.memory.embedding import EmbeddingProvider
from xmclaw.providers.tool.base import ToolProvider
from xmclaw.utils.log import get_logger

_log = get_logger(__name__)


class CodebaseToolProvider(ToolProvider):
    """Tools: ``codebase_search``, ``codebase_conventions``.

    Requires a :class:`CodebaseStore` instance (typically shared with the
    :class:`CodebaseIndexer` that built the index).
    """

    name = "codebase"

    def __init__(
        self,
        store: CodebaseStore,
        *,
        embedder: EmbeddingProvider | None = None,
    ) -> None:
        self._store = store
        self._embedder = embedder

    def list_tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="codebase_search",
                description=(
                    "Search the indexed codebase for symbols, functions, classes, or "
                    "concepts. Returns relevant code snippets with file paths and line numbers. "
                    "Use this BEFORE falling back to bash/grep when working inside a known project."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "What to search for — can be a symbol name, concept, or natural language.",
                        },
                        "project_root": {
                            "type": "string",
                            "description": "Optional absolute path to project root to scope the search.",
                        },
                        "symbol_kind": {
                            "type": "string",
                            "description": "Optional filter: 'function', 'class', 'method', 'interface'.",
                        },
                        "k": {
                            "type": "integer",
                            "description": "Max results to return (default 10).",
                            "default": 10,
                        },
                    },
                    "required": ["query"],
                },
            ),
            ToolSpec(
                name="codebase_conventions",
                description=(
                    "Return auto-extracted conventions for a project: language, test framework, "
                    "linter, architecture pattern, key files, etc. Use this to align with the "
                    "project's existing style before proposing changes."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "project_root": {
                            "type": "string",
                            "description": "Absolute path to project root.",
                        },
                    },
                    "required": ["project_root"],
                },
            ),
        ]

    async def invoke(self, call: ToolCall) -> ToolResult:
        name = call.name
        args = call.args

        if name == "codebase_search":
            return await self._search(args)
        if name == "codebase_conventions":
            return self._conventions(args)
        return ToolResult(error=f"Unknown codebase tool: {name}")

    async def _search(self, args: dict[str, Any]) -> ToolResult:
        query: str = args.get("query", "")
        project_root: str | None = args.get("project_root")
        symbol_kind: str | None = args.get("symbol_kind")
        k: int = int(args.get("k", 10))

        if not query:
            return ToolResult(error="query is required")

        prefix = f"{project_root}/" if project_root else None
        results: list[dict[str, Any]] = []

        # 1. Try semantic search if embedder is available.
        if self._embedder is not None and self._embedder.is_available():
            try:
                embeddings = await self._embedder.embed([query])
                if embeddings:
                    vec_results = self._store.search_semantic(
                        embeddings[0], k=k,
                        relpath_prefix=prefix, symbol_kind=symbol_kind,
                    )
                    results.extend(vec_results)
            except Exception as exc:
                _log.debug("semantic_search_failed: %s", exc)

        # 2. Fallback / supplement with FTS5.
        if len(results) < k:
            need = k - len(results)
            text_results = self._store.search_text(
                query, k=need,
                relpath_prefix=prefix, symbol_kind=symbol_kind,
            )
            # Deduplicate by id.
            seen = {r["id"] for r in results}
            for r in text_results:
                if r["id"] not in seen:
                    results.append(r)

        # 3. Symbol name exact/prefix match as last resort.
        if len(results) < k // 2:
            sym_results = self._store.search_symbol(query, relpath_prefix=prefix)
            seen = {r["id"] for r in results}
            for r in sym_results:
                if r["id"] not in seen and len(results) < k:
                    results.append(r)

        if not results:
            return ToolResult(output="No results found in the codebase index.")

        lines: list[str] = [f"Found {len(results)} result(s):", ""]
        for r in results:
            loc = f"{r['relpath']}:{r['start_line']}"
            sig = f" | {r['signature']}" if r.get("signature") else ""
            kind = f" ({r['symbol_kind']})" if r.get("symbol_kind") else ""
            snippet = r["text"][:800]
            lines.append(f"--- {loc}{kind}{sig} ---")
            lines.append(snippet)
            lines.append("")

        return ToolResult(output="\n".join(lines))

    def _conventions(self, args: dict[str, Any]) -> ToolResult:
        project_root: str = args.get("project_root", "")
        if not project_root:
            return ToolResult(error="project_root is required")
        try:
            conv = extract_conventions(self._store, project_root)
            return ToolResult(output=render_conventions(conv))
        except Exception as exc:
            return ToolResult(error=f"Failed to extract conventions: {exc}")
