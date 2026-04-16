"""Step 3: Enhance CLI with gene/skill/agent/config commands."""
content = '''"""CLI entry point for XMclaw."""
import asyncio
import json
import os
import typer
from pathlib import Path
from xmclaw.daemon.lifecycle import start_daemon, stop_daemon, daemon_status
from xmclaw.cli.client import run_cli_client
from xmclaw.utils.paths import BASE_DIR, get_agent_dir
from xmclaw.tools.registry import ToolRegistry
from xmclaw.genes.manager import GeneManager
from xmclaw.memory.manager import MemoryManager
from xmclaw.llm.router import LLMRouter

app = typer.Typer(help="XMclaw - Local-first AI Agent runtime")
sub_gene = typer.Typer(help="Gene management")
sub_skill = typer.Typer(help="Skill management")
sub_agent = typer.Typer(help="Agent management")
sub_config = typer.Typer(help="Configuration management")
app.add_typer(sub_gene, name="gene")
app.add_typer(sub_skill, name="skill")
app.add_typer(sub_agent, name="agent")
app.add_typer(sub_config, name="config")


# ── Daemon ──────────────────────────────────────────────────────────────────

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


# ── Chat ───────────────────────────────────────────────────────────────────

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


# ── Tasks ──────────────────────────────────────────────────────────────────

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
        flag = t.get("status", "pending")
        title = t.get("title", "")
        desc = t.get("description", "")
        typer.echo(f"[{flag}] {title}" + (f": {desc}" if desc else ""))


@app.command()
def task_create(
    title: str = typer.Argument(..., help="Task title"),
    description: str = typer.Option("", "--desc", "-d", help="Task description"),
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


# ── Evolution ───────────────────────────────────────────────────────────────

@app.command()
def evolution_status():
    """Show evolution system status."""
    genes_dir = BASE_DIR / "shared" / "genes"
    skills_dir = BASE_DIR / "shared" / "skills"
    gene_count = len(list(genes_dir.glob("gene_*.py"))) if genes_dir.exists() else 0
    skill_count = len(list(skills_dir.glob("skill_*.py"))) if skills_dir.exists() else 0
    typer.echo(f"Genes: {gene_count}")
    typer.echo(f"Skills: {skill_count}")


# ── Memory ─────────────────────────────────────────────────────────────────

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
                        results.append(str(fpath.relative_to(agent_dir)))
                except Exception:
                    pass
    if results:
        typer.echo(f"Found in {len(results)} file(s):")
        for r in results:
            typer.echo(f"  - {r}")
    else:
        typer.echo("No results found.")


# ── Gene ────────────────────────────────────────────────────────────────────

@sub_gene.command("list")
def gene_list(agent_id: str = typer.Option("default", "--agent", "-a")):
    """List all genes for an agent."""
    gm = GeneManager(agent_id)
    genes = gm.get_all()
    if not genes:
        typer.echo("No genes found.")
        return
    for g in genes:
        gene_id = g.get("gene_id", "?")
        name = g.get("name", "?")
        priority = g.get("priority", 0)
        trigger = g.get("trigger", "")
        enabled = "enabled" if g.get("enabled", True) else "DISABLED"
        typer.echo(f"[{enabled}] {gene_id} | {name} | priority={priority} | trigger={trigger}")


@sub_gene.command("show")
def gene_show(gene_id: str = typer.Argument(..., help="Gene ID to show")):
    """Show detailed info for a gene."""
    gm = GeneManager()
    gene = gm.get_gene(gene_id)
    if not gene:
        typer.echo(f"Gene '{gene_id}' not found.")
        return
    typer.echo(json.dumps(gene, indent=2, ensure_ascii=False))


@sub_gene.command("match")
def gene_match(
    input_text: str = typer.Argument(..., help="Input text to match against genes"),
    agent_id: str = typer.Option("default", "--agent", "-a"),
    intents: str = typer.Option("", "--intents", help="Comma-separated intents"),
):
    """Simulate gene matching for a given input."""
    gm = GeneManager(agent_id)
    intent_list = [i.strip() for i in intents.split(",") if i.strip()] or None
    matched = gm.match(input_text, intents=intent_list)
    if not matched:
        typer.echo("No genes matched.")
        return
    typer.echo(f"Matched {len(matched)} gene(s):")
    for g in matched:
        typer.echo(f"  - {g.get('gene_id')}: {g.get('name')}")


# ── Skill ───────────────────────────────────────────────────────────────────

@sub_skill.command("list")
def skill_list():
    """List all generated skills in shared/skills/."""
    skills_dir = BASE_DIR / "shared" / "skills"
    if not skills_dir.exists():
        typer.echo("No skills directory found.")
        return
    files = sorted(skills_dir.glob("skill_*.py"))
    if not files:
        typer.echo("No skills found.")
        return
    typer.echo(f"Total: {len(files)} skill(s)")
    for f in files:
        typer.echo(f"  - {f.stem}")


@sub_skill.command("show")
def skill_show(skill_id: str = typer.Argument(..., help="Skill name to show")):
    """Show skill source code."""
    skills_dir = BASE_DIR / "shared" / "skills"
    candidates = list(skills_dir.glob(f"*{skill_id}*.py"))
    if not candidates:
        typer.echo(f"Skill '{skill_id}' not found.")
        return
    content = candidates[0].read_text(encoding="utf-8")
    typer.echo(content)


# ── Agent ───────────────────────────────────────────────────────────────────

@sub_agent.command("list")
def agent_list():
    """List all agent directories."""
    agents_dir = BASE_DIR / "agents"
    if not agents_dir.exists():
        typer.echo("No agents directory found.")
        return
    agents = [d.name for d in agents_dir.iterdir() if d.is_dir() and not d.name.startswith(".")]
    if not agents:
        typer.echo("No agents found.")
        return
    typer.echo(f"Agents: {', '.join(agents)}")


@sub_agent.command("create")
def agent_create(name: str = typer.Argument(..., help="New agent name")):
    """Create a new agent directory with