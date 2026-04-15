"""CLI entry point for XMclaw."""
import asyncio
import json
import os
import typer
from pathlib import Path
from xmclaw.daemon.lifecycle import start_daemon, stop_daemon, daemon_status
from xmclaw.cli.client import run_cli_client
from xmclaw.utils.paths import BASE_DIR, get_agent_dir

app = typer.Typer(help="XMclaw - Local-first AI Agent runtime")


@app.command()
def start():
    """Start the XMclaw daemon."""
    start_daemon()


@app.command()
def stop():
    """Stop the XMclaw daemon."""
    stop_daemon()


@app.command()
def status():
    """Check daemon status."""
    daemon_status()


@app.command()
def chat(
    agent_id: str = typer.Option("default", "--agent", "-a", help="Agent ID"),
    plan: bool = typer.Option(False, "--plan", "-p", help="Enable plan mode"),
):
    """Start an interactive chat session."""
    if plan:
        asyncio.run(run_cli_client(agent_id, plan_mode=True))
    else:
        asyncio.run(run_cli_client(agent_id))


@app.command()
def task_list(agent_id: str = typer.Option("default", "--agent", "-a")):
    """List active tasks for an agent."""
    agent_dir = get_agent_dir(agent_id)
    path = agent_dir / "tasks.json"
    if not path.exists():
        typer.echo("No tasks found.")
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    for t in data:
        typer.echo(f"[{t.get('status', 'pending')}] {t.get('title', '')}: {t.get('description', '')}")


@app.command()
def task_create(
    title: str,
    description: str = "",
    agent_id: str = typer.Option("default", "--agent", "-a"),
):
    """Create a new task."""
    agent_dir = get_agent_dir(agent_id)
    path = agent_dir / "tasks.json"
    tasks = []
    if path.exists():
        tasks = json.loads(path.read_text(encoding="utf-8"))
    tasks.append({"title": title, "description": description, "status": "pending"})
    path.write_text(json.dumps(tasks, indent=2, ensure_ascii=False), encoding="utf-8")
    typer.echo(f"Task created: {title}")


@app.command()
def evolution_status():
    """Show evolution system status."""
    genes_dir = BASE_DIR / "shared" / "genes"
    skills_dir = BASE_DIR / "shared" / "skills"
    gene_count = len(list(genes_dir.glob("gene_*.py"))) if genes_dir.exists() else 0
    skill_count = len(list(skills_dir.glob("skill_*.py"))) if skills_dir.exists() else 0
    typer.echo(f"Genes: {gene_count}")
    typer.echo(f"Skills: {skill_count}")


@app.command()
def memory_search(query: str, agent_id: str = typer.Option("default", "--agent", "-a")):
    """Search agent memory files."""
    agent_dir = get_agent_dir(agent_id)
    results = []
    for root, _, filenames in os.walk(agent_dir):
        for fname in filenames:
            if fname.endswith(".md") or fname.endswith(".jsonl"):
                fpath = Path(root) / fname
                try:
                    text = fpath.read_text(encoding="utf-8")
                    if query.lower() in text.lower():
                        rel = fpath.relative_to(agent_dir).as_posix()
                        results.append(rel)
                except Exception:
                    pass
    if not results:
        typer.echo("No results found.")
    else:
        for r in results[:20]:
            typer.echo(f"  {r}")


@app.command()
def config_show(agent_id: str = typer.Option("default", "--agent", "-a")):
    """Show agent configuration."""
    path = get_agent_dir(agent_id) / "agent.json"
    if not path.exists():
        typer.echo("No config found.")
        return
    typer.echo(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    app()
