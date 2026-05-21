"""Codebase CLI — ``xmclaw codebase {index,search,status,conventions}``."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import typer

from xmclaw.cognition.codebase_index import CodebaseIndexer
from xmclaw.cognition.codebase_index.conventions import extract_conventions, render_conventions
from xmclaw.cognition.codebase_index.store import CodebaseStore
from xmclaw.providers.memory.embedding import build_embedding_provider
from xmclaw.utils.paths import data_dir

codebase_app = typer.Typer(help="Codebase semantic indexing and search")


def _store() -> CodebaseStore:
    store_path = data_dir() / "v2" / "codebase" / "index.db"
    return CodebaseStore(store_path)


def _indexer() -> CodebaseIndexer:
    from xmclaw.daemon.factory import load_config
    cfg = load_config()
    embedder = build_embedding_provider(cfg)
    store_path = data_dir() / "v2" / "codebase" / "index.db"
    return CodebaseIndexer(store_path, embedder=embedder)


@codebase_app.command("index")
def codebase_index(
    path: Path = typer.Argument(..., help="Project root directory to index"),
    name: str | None = typer.Option(None, help="Project display name (default: directory name)"),
    force: bool = typer.Option(False, "--force", help="Force full re-index (ignore cached hashes)"),
) -> None:
    """Index a project for semantic code search."""
    import asyncio
    if not path.exists():
        typer.secho(f"Path does not exist: {path}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    indexer = _indexer()
    try:
        result = asyncio.run(indexer.index_project(path, name=name, force_full=force))
        typer.secho(
            f"Indexed {result['indexed_files']} files, {result['indexed_chunks']} chunks "
            f"({result['skipped_files']} skipped) in {result['elapsed_seconds']:.1f}s",
            fg=typer.colors.GREEN,
        )
    finally:
        indexer.close()


@codebase_app.command("search")
def codebase_search(
    query: str = typer.Argument(..., help="Search query — symbol name, concept, or natural language"),
    project: str | None = typer.Option(None, "--project", help="Scope to a project root path"),
    kind: str | None = typer.Option(None, "--kind", help="Filter by symbol kind (function, class, method, interface)"),
    k: int = typer.Option(10, "--limit", help="Max results"),
) -> None:
    """Search the indexed codebase."""
    store = _store()
    try:
        prefix = f"{project}/" if project else None
        results = store.search_text(query, k=k, relpath_prefix=prefix, symbol_kind=kind)
        if not results:
            typer.echo("No results found.")
            return
        for r in results:
            loc = f"{r['relpath']}:{r['start_line']}"
            sig = f" | {r['signature']}" if r.get("signature") else ""
            kind_label = f" ({r['symbol_kind']})" if r.get("symbol_kind") else ""
            typer.secho(f"{loc}{kind_label}{sig}", fg=typer.colors.CYAN)
            snippet = r["text"][:600]
            typer.echo(snippet)
            typer.echo("---")
    finally:
        store.close()


@codebase_app.command("status")
def codebase_status() -> None:
    """Show indexed projects and stats."""
    store = _store()
    try:
        projects = store.list_projects()
        if not projects:
            typer.echo("No indexed projects yet. Run `xmclaw codebase index <path>` to start.")
            return
        for p in projects:
            stats = store.project_stats(p["root_path"])
            typer.secho(f"{p['name']}", fg=typer.colors.GREEN, bold=True)
            typer.echo(f"  root: {p['root_path']}")
            typer.echo(f"  files tracked: {p['file_count']}")
            if stats:
                typer.echo(f"  files indexed: {stats.get('indexed_files', 'N/A')}")
                typer.echo(f"  chunks: {stats.get('indexed_chunks', 'N/A')}")
    finally:
        store.close()


@codebase_app.command("conventions")
def codebase_conventions(
    path: Path = typer.Argument(..., help="Project root directory"),
) -> None:
    """Show auto-extracted conventions for a project."""
    store = _store()
    try:
        conv = extract_conventions(store, str(path.resolve()))
        typer.echo(render_conventions(conv))
    finally:
        store.close()
