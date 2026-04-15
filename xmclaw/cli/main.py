"""CLI entry point for XMclaw."""
import asyncio
import typer
from xmclaw.daemon.lifecycle import start_daemon, stop_daemon, daemon_status
from xmclaw.cli.client import run_cli_client

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
):
    """Start an interactive chat session."""
    asyncio.run(run_cli_client(agent_id))


if __name__ == "__main__":
    app()
