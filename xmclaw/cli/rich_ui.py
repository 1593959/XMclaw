"""Rich-based UI components."""
from rich.console import Console
from rich.panel import Panel
from rich.live import Live
from rich.spinner import Spinner

console = Console()


def print_banner():
    console.print(
        Panel.fit(
            "[bold cyan]XMclaw[/bold cyan] - Local-first AI Agent\n"
            "Type your message and press Enter. Use /quit to exit.",
            title="Welcome",
            border_style="cyan",
        )
    )


def print_user(text: str):
    console.print(f"[bold green]You:[/bold green] {text}")


def print_agent(text: str):
    console.print(f"[bold blue]XMclaw:[/bold blue] {text}")


def print_tool(name: str, result: str):
    console.print(f"[dim][Tool {name}]: {result[:200]}[/dim]")


def create_spinner(text: str = "Thinking..."):
    return Spinner("dots", text=text)
