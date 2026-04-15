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


def print_agent(text: str, end: str = "\n", flush: bool = False):
    console.print(text, end=end)


def print_tool(name: str, result: str):
    console.print(
        Panel(
            f"[dim]{result[:500]}[/dim]",
            title=f"[yellow]Tool: {name}[/yellow]",
            border_style="yellow",
        )
    )


def print_state(state: str, thought: str):
    console.print(
        f"[dim italic]State: {state} | {thought[:100]}...[/dim italic]",
        highlight=False,
    )


def print_ask_user(question: str):
    console.print(
        Panel(
            f"[bold magenta]{question}[/bold magenta]",
            title="[magenta]XMclaw 询问[/magenta]",
            border_style="magenta",
        )
    )


def create_spinner(text: str = "Thinking..."):
    return Spinner("dots", text=text)
