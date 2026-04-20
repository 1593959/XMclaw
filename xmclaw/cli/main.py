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
from xmclaw.genes.manager import GeneManager

# Rich formatting for CLI output
try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    _RICH = True
except ImportError:
    _RICH = False

app = typer.Typer(
    help="XMclaw - Local-first AI Agent runtime",
    rich_markup_mode="rich",
)
sub_gene = typer.Typer(help="Gene management", rich_markup_mode="rich")
sub_skill = typer.Typer(help="Skill management", rich_markup_mode="rich")
sub_agent = typer.Typer(help="Agent management", rich_markup_mode="rich")
sub_config = typer.Typer(help="Configuration management", rich_markup_mode="rich")
app.add_typer(sub_gene, name="gene")
app.add_typer(sub_skill, name="skill")
app.add_typer(sub_agent, name="agent")
app.add_typer(sub_config, name="config")


# ── Daemon ──────────────────────────────────────────────────────────────────

@app.command()
def start(no_browser: bool = False):
    """Start the XMclaw daemon (auto-opens browser by default)."""
    start_daemon(open_browser=not no_browser)


@app.command()
def stop():
    """Stop the XMclaw daemon."""
    from xmclaw.daemon.lifecycle import is_running
    running = is_running()
    stop_daemon()
    if _RICH:
        console = Console()
        if running:
            console.print("[green]Daemon stopped[/green]")
        else:
            console.print("[yellow]Daemon was not running[/yellow]")
    else:
        print("Daemon stopped" if running else "Daemon was not running")


@app.command()
def restart():
    """Restart the daemon (stop then start)."""
    stop()
    start_daemon(open_browser=False)


@app.command()
def status():
    """Show daemon status, PID, URL and recent errors."""
    from xmclaw.daemon.lifecycle import is_running, PID_FILE, _get_daemon_url
    running = is_running()
    url = _get_daemon_url()

    if _RICH:
        console = Console()
        if running:
            pid = PID_FILE.read_text().strip() if PID_FILE.exists() else "?"
            console.print(Panel(
                f"[green]🟢 运行中[/green]  [bold]PID {pid}[/bold]\n"
                f"[cyan]{url}[/cyan]",
                title="XMclaw Daemon",
                border_style="green",
            ))
        else:
            console.print(Panel(
                "[yellow]🟡 未运行[/yellow]",
                title="XMclaw Daemon",
                border_style="yellow",
            ))
    else:
        if running:
            pid = PID_FILE.read_text().strip() if PID_FILE.exists() else "?"
            print(f"Running  PID={pid}  {url}")
        else:
            print(f"Not running  {url}")


@app.command()
def logs(lines: int = typer.Option(50, "--lines", "-n", help="Number of lines to show")):
    """Show daemon log (last N lines)."""
    from xmclaw.daemon.lifecycle import BASE_DIR
    log_path = BASE_DIR / "logs" / "daemon.log"
    if not log_path.exists():
        print("No daemon log found.")
        return
    content = log_path.read_text(encoding="utf-8", errors="replace")
    all_lines = content.splitlines()
    tail = all_lines[-lines:]
    for line in tail:
        # Highlight errors in red
        if "error" in line.lower() or "traceback" in line.lower():
            print(f"[ERROR] {line}" if not _RICH else f"[red]ERROR[/red] {line}")
        else:
            print(line)


def _is_daemon_running() -> bool:
    from xmclaw.daemon.lifecycle import is_running
    return is_running()


def _show_quick_status(console) -> None:
    """Show gene/skill counts and config summary."""
    from xmclaw.utils.paths import BASE_DIR

    genes_dir = BASE_DIR / "shared" / "genes"
    skills_dir = BASE_DIR / "shared" / "skills"
    gene_count = len(list(genes_dir.glob("gene_*.py"))) if genes_dir.exists() else 0
    skill_count = len(list(skills_dir.glob("skill_*.py"))) if skills_dir.exists() else 0

    # Check API key presence
    try:
        from xmclaw.daemon.config import DaemonConfig
        cfg = DaemonConfig.load()
        anthropic_ok = bool(cfg.llm.get("anthropic", {}).get("api_key"))
        openai_ok = bool(cfg.llm.get("openai", {}).get("api_key"))
    except Exception:
        anthropic_ok = openai_ok = False

    table = Table(show_header=True, header_style="bold", box=None)
    table.add_column("项目", style="cyan")
    table.add_column("状态", style="green")
    table.add_row("基因 (Genes)", str(gene_count))
    table.add_row("技能 (Skills)", str(skill_count))
    table.add_row("Anthropic API Key", "[green]✓ 已配置[/green]" if anthropic_ok else "[yellow]✗ 未配置[/yellow]")
    table.add_row("OpenAI API Key", "[green]✓ 已配置[/green]" if openai_ok else "[dim]✗ 未配置[/dim]")
    console.print(table)


@app.command()
def events(
    limit: int = typer.Option(20, "--limit", "-n", help="Number of recent events to show"),
    etype: str = typer.Option(None, "--type", "-t", help="Filter by event type"),
):
    """Show recent events from the event bus."""
    try:
        from xmclaw.core.event_bus import get_event_bus
        bus = get_event_bus()
        events = bus.get_history(event_type=etype, limit=limit)
        stats = bus.get_stats()

        if _RICH:
            from rich.console import Console
            from rich.table import Table
            console = Console()
            console.print(f"[dim]Total: {stats['total_events']} events, "
                          f"{stats['subscriber_count']} subscribers[/dim]")
            table = Table(show_header=True, header_style="bold", box=None)
            table.add_column("Type", style="cyan", width=30)
            table.add_column("Source", width=20)
            table.add_column("Time", width=22)
            table.add_column("Payload", width=40)
            for e in reversed(events):
                ts = e.timestamp[:19] if e.timestamp else ""
                payload = str(e.payload)[:40] if e.payload else ""
                table.add_row(e.event_type, e.source, ts, payload)
            console.print(table)
        else:
            for e in reversed(events):
                print(f"[{e.event_type}] {e.source} | {e.timestamp[:19]} | {e.payload}")
    except Exception as e:
        typer.echo(f"Error: {e}")


@app.command()
def doctor():
    """Run a quick diagnostic to identify setup issues.

    Checks: config file, API keys, port availability, Playwright, disk space.
    """
    checks = []
    errors = []
    warnings = []

    # 1. Config file
    from xmclaw.utils.paths import BASE_DIR
    config_path = BASE_DIR / "daemon" / "config.json"
    if not config_path.exists():
        errors.append("Config file not found. Run: xmclaw config init")
    else:
        checks.append(f"✅ Config: {config_path}")
        try:
            from xmclaw.daemon.config import DaemonConfig
            cfg = DaemonConfig.load()
            anthropic_key = cfg.llm.get("anthropic", {}).get("api_key", "")
            openai_key = cfg.llm.get("openai", {}).get("api_key", "")
            if anthropic_key:
                checks.append("✅ Anthropic API Key: configured")
            else:
                warnings.append("⚠️  Anthropic API Key: empty — set with xmclaw config init")
            if openai_key:
                checks.append("✅ OpenAI API Key: configured")
            else:
                warnings.append("⚠️  OpenAI API Key: empty — set with xmclaw config init")
        except Exception as e:
            errors.append(f"❌ Config parse error: {e}")

    # 2. Port availability
    import socket
    port = 8765
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        result = sock.connect_ex(("127.0.0.1", port))
        if result == 0:
            warnings.append(f"⚠️  Port {port} is already in use — daemon may already be running")
        else:
            checks.append(f"✅ Port {port}: available")
        sock.close()
    except Exception:
        pass

    # 3. Playwright
    try:
        from playwright.sync_api import sync_playwright
        checks.append("✅ Playwright: installed")
    except ImportError:
        warnings.append("⚠️  Playwright: not installed — browser automation unavailable (pip install playwright && playwright install chromium)")

    # 4. Shared directories
    shared = BASE_DIR / "shared"
    if shared.exists():
        gene_count = len(list((shared / "genes").glob("gene_*.py"))) if (shared / "genes").exists() else 0
        skill_count = len(list((shared / "skills").glob("skill_*.py"))) if (shared / "skills").exists() else 0
        checks.append(f"✅ Shared dirs: {gene_count} genes, {skill_count} skills")
    else:
        warnings.append("⚠️  shared/ directory not created yet — will be created on first run")

    # 5. Python version
    import sys
    if sys.version_info >= (3, 10):
        checks.append(f"✅ Python: {sys.version_info.major}.{sys.version_info.minor}")
    else:
        errors.append(f"❌ Python {sys.version_info.major}.{sys.version_info.minor} — requires 3.10+")

    # Output
    all_items = checks + warnings + errors
    try:
        from rich.console import Console
        from rich.table import Table
        console = Console()
        table = Table(show_header=True, header_style="bold", box=None, title="XMclaw Doctor")
        table.add_column("Check", style="cyan", width=60)
        for item in all_items:
            style = "green" if item.startswith("✅") else ("yellow" if item.startswith("⚠️") else "red")
            table.add_row(f"[{style}]{item}[/{style}]")
        console.print(table)
        if errors:
            console.print(f"\n[red]❌ {len(errors)} error(s) found. Fix before running.[/red]")
        elif warnings:
            console.print(f"\n[yellow]🟡 {len(warnings)} warning(s). XMclaw may run but some features unavailable.[/yellow]")
        else:
            console.print("\n[green]✅ All checks passed. XMclaw is ready to run![/green]")
    except Exception:
        for item in all_items:
            print(item)
        if errors:
            print(f"\nERRORS: {errors}")
        elif warnings:
            print(f"\nWARNINGS: {warnings}")


@app.command()
def completion():
    """Print shell completion script instructions."""
    try:
        from rich.console import Console
        console = Console()
    except ImportError:
        console = None

    script = """
# Bash — 添加到 ~/.bashrc:
eval "$(_XMCLAW_COMPLETE=bash_source xmclaw)"

# Zsh — 添加到 ~/.zshrc:
eval "$(_XMCLAW_COMPLETE=zsh_source xmclaw)"

# Fish — 添加到 ~/.config/fish/completions/xmclaw.fish:
_XMCLAW_COMPLETE=fish_source xmclaw > ~/.config/fish/completions/xmclaw.fish
"""
    if console:
        console.print(Panel(script, title="🖥️ Shell 自动补全", border_style="cyan"))
    else:
        print(script)


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
    input_text: str = typer.Argument(..., help="Input text to match genes against"),
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
def skill_show(skill_name: str = typer.Argument(..., help="Skill name to show")):
    """Show skill source code."""
    skills_dir = BASE_DIR / "shared" / "skills"
    candidates = list(skills_dir.glob(f"*{skill_name}*.py"))
    if not candidates:
        candidates = list(skills_dir.glob("skill_*.py"))
        candidates = [f for f in candidates if skill_name.lower() in f.stem.lower()]
    if not candidates:
        typer.echo(f"Skill '{skill_name}' not found.")
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
    """Create a new agent directory with basic config files."""
    agents_dir = BASE_DIR / "agents"
    agent_dir = agents_dir / name
    if agent_dir.exists():
        typer.echo(f"Agent '{name}' already exists.")
        return
    agent_dir.mkdir(parents=True, exist_ok=True)
    workspace = agent_dir / "workspace"
    workspace.mkdir(exist_ok=True)
    config_path = agent_dir / "agent.json"
    if not config_path.exists():
        default_config = {
            "agent_id": name,
            "model": "gpt-4o",
            "fallback_model": "claude-sonnet-4-20250514",
            "temperature": 0.7,
            "max_tokens": 4096,
        }
        config_path.write_text(json.dumps(default_config, indent=2, ensure_ascii=False), encoding="utf-8")
    typer.echo(f"Agent '{name}' created at {agent_dir.relative_to(BASE_DIR)}")


@sub_agent.command("info")
def agent_info(name: str = typer.Argument("default", help="Agent name")):
    """Show agent info and its markdown configs."""
    agent_dir = BASE_DIR / "agents" / name
    if not agent_dir.exists():
        typer.echo(f"Agent '{name}' not found.")
        return
    typer.echo(f"Agent: {name}")
    typer.echo(f"Dir: {agent_dir}")
    for fname in ["agent.json", "SOUL.md", "PROFILE.md", "AGENTS.md"]:
        p = agent_dir / fname
        exists = "存在" if p.exists() else "不存在"
        typer.echo(f"  {fname}: {exists}")


# ── Config ─────────────────────────────────────────────────────────────────

@sub_config.command("init")
def config_init():
    """Run the interactive first-run setup wizard (API key configuration)."""
    from xmclaw.daemon.lifecycle import run_setup_wizard
    run_setup_wizard()


@sub_config.command("show")
def config_show():
    """Show current configuration (reads daemon/config.json)."""
    try:
        from xmclaw.daemon.config import DaemonConfig
        cfg = DaemonConfig.load()
        # Mask secrets before displaying
        safe = cfg.mask_secrets()
        typer.echo(json.dumps(safe.__dict__, indent=2, ensure_ascii=False))
    except Exception as e:
        typer.echo(f"Error loading config: {e}")


@sub_config.command("set")
def config_set(
    key: str = typer.Argument(..., help="Config key, use dot notation for nested (e.g. evolution.interval_minutes)"),
    value: str = typer.Argument(..., help="Config value"),
):
    """Set a config value in daemon/config.json.

    Supports dot-notation for nested keys:
      xmclaw config set evolution.interval_minutes 15
      xmclaw config set llm.default_provider openai
      xmclaw config set tools.browser_headless true
    """
    from xmclaw.daemon.config import DaemonConfig
    try:
        cfg = DaemonConfig.load()
        cfg_dict = cfg.__dict__

        # Parse dot-notation
        parts = key.split(".")
        target = cfg_dict
        for part in parts[:-1]:
            if part not in target or not isinstance(target[part], dict):
                typer.echo(f"Error: '{key}' — section '{part}' not found or not a dict.")
                return
            target = target[part]

        leaf_key = parts[-1]
        if leaf_key not in target:
            typer.echo(f"Warning: key '{key}' not in default config, adding it.")

        # Infer type
        if value.lower() in ("true", "yes", "1"):
            typed = True
        elif value.lower() in ("false", "no", "0"):
            typed = False
        elif value.replace(".", "", 1).isdigit() and "." in value:
            typed = float(value)
        elif value.isdigit():
            typed = int(value)
        else:
            typed = value

        target[leaf_key] = typed

        # Write back
        config_path = BASE_DIR / "daemon" / "config.json"
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(cfg_dict, f, indent=2, ensure_ascii=False)
        typer.echo(f"✅ Set {key} = {typed!r}")
    except Exception as e:
        typer.echo(f"Error: {e}")


@sub_config.command("env")
def config_env():
    """Print useful environment variable overrides and how to set them.

    Environment variables with prefix XMC__ override daemon/config.json values.
    This is useful for container deployments or CI environments.
    """
    lines = [
        "# XMclaw Environment Variable Overrides",
        "# Prefix: XMC__  Separator between levels: __ (double underscore)",
        "",
        "# Example — set these in your shell or .env file:",
        "",
        "# LLM API Keys",
        'export XMC__llm__anthropic__api_key="sk-ant-..."',
        'export XMC__llm__openai__api_key="sk-..."',
        "",
        "# Evolution control",
        'export XMC__evolution__enabled="false"',
        'export XMC__evolution__interval_minutes="60"',
        'export XMC__evolution__vfm_threshold="7.0"',
        "",
        "# Gateway",
        'export XMC__gateway__port="8765"',
        "",
        "# Tools",
        'export XMC__tools__bash_timeout="600"',
        'export XMC__tools__browser_headless="true"',
        "",
        "# Memory",
        'export XMC__memory__session_retention_days="30"',
    ]
    msg = "\n".join(lines)
    try:
        from rich.console import Console
        console = Console()
        console.print(Panel(msg, title="🌍 Environment Variables", border_style="green", expand=False))
    except Exception:
        print(msg)


@sub_config.command("reset")
def config_reset(
    key: str = typer.Argument(..., help="Config key to reset to default (use 'all' to reset everything)"),
):
    """Reset a config key to its default value, or reset the entire config."""
    from xmclaw.daemon.config import DaemonConfig

    if key == "all":
        cfg = DaemonConfig.default()
        config_path = BASE_DIR / "daemon" / "config.json"
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(cfg.__dict__, f, indent=2, ensure_ascii=False)
        typer.echo("✅ Config reset to defaults.")
        return

    cfg = DaemonConfig.load()
    cfg_dict = cfg.__dict__

    parts = key.split(".")
    target = cfg_dict
    for part in parts[:-1]:
        if part not in target or not isinstance(target[part], dict):
            typer.echo(f"Error: key '{key}' not found.")
            return
        target = target[part]

    leaf_key = parts[-1]
    if leaf_key not in target:
        typer.echo(f"Key '{key}' not found.")
        return
    default_cfg = DaemonConfig.default().__dict__
    for part in parts[:-1]:
        default_cfg = default_cfg[part]
    default_val = default_cfg.get(leaf_key, None)
    target[leaf_key] = default_val

    config_path = BASE_DIR / "daemon" / "config.json"
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(cfg_dict, f, indent=2, ensure_ascii=False)
    typer.echo(f"✅ Reset {key} to default: {default_val!r}")


# ── Test ───────────────────────────────────────────────────────────────────

@app.command()
def test(module: str = typer.Option("all", "--module", "-m", help="Module to test")):
    """Run tests."""
    import subprocess
    if module == "all":
        result = subprocess.run(["python", "-m", "pytest", "tests/", "-v"], cwd=BASE_DIR)
    else:
        result = subprocess.run(["python", "-m", "pytest", f"tests/test_{module}.py", "-v"], cwd=BASE_DIR)
    raise typer.Exit(code=result.returncode)


# ── Main ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app()
