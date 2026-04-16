"""CLI entry point for XMclaw."""
import asyncio
import json
import os
import typer
from pathlib import Path
from xmclaw.daemon.lifecycle import start_daemon, stop_daemon, daemon_status
from xmclaw.cli.client import run_cli_client
from xmclaw.utils.paths import BASE_DIR, get_agent_dir
from xmclaw.tools.registry import ToolRegistry

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
def config_show():
    """Show daemon configuration."""
    path = BASE_DIR / "daemon" / "config.json"
    if not path.exists():
        typer.echo("No config found.")
        return
    typer.echo(path.read_text(encoding="utf-8"))



@app.command("test")
def test_cmd(
    action: str = typer.Option("run_all", "--action", "-a", help="generate | run | run_all"),
    target: str = typer.Option("", "--target", "-t", help="Target file path for generate/run"),
):
    """Auto-generate and run tests."""
    async def _run():
        reg = ToolRegistry()
        await reg.load_all()
        args = {"action": action}
        if target:
            args["target"] = target
        result = await reg.execute("test", args)
        typer.echo(result)
    asyncio.run(_run())


@app.command("computer-use")
def computer_use(
    action: str = typer.Argument(..., help="screenshot | click | move | type | keypress | scroll | drag"),
    x: int = typer.Option(0, "--x", help="X coordinate"),
    y: int = typer.Option(0, "--y", help="Y coordinate"),
    end_x: int = typer.Option(0, "--end-x", help="End X for drag"),
    end_y: int = typer.Option(0, "--end-y", help="End Y for drag"),
    text: str = typer.Option("", "--text", "-t", help="Text to type"),
    key: str = typer.Option("", "--key", "-k", help="Key or combo (e.g. ctrl+c)"),
    scroll_y: int = typer.Option(0, "--scroll-y", help="Scroll amount"),
):
    """Remote control the computer desktop."""
    async def _run():
        reg = ToolRegistry()
        await reg.load_all()
        args = {"action": action}
        if action in ("click", "move", "scroll", "drag"):
            args["x"] = x
            args["y"] = y
        if action == "drag":
            args["end_x"] = end_x
            args["end_y"] = end_y
        if action == "type":
            args["text"] = text
        if action == "keypress":
            args["key"] = key
        if action == "scroll":
            args["scroll_y"] = scroll_y
        result = await reg.execute("computer_use", args)
        typer.echo(result)
    asyncio.run(_run())


if __name__ == "__main__":
    app()
