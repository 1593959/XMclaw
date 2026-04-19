"""Rich-based UI components."""
import sys
from rich.console import Console
from rich.panel import Panel
from rich.live import Live
from rich.spinner import Spinner

# Windows GBK 环境需要安全模式
_console = Console(safe_box=True)

def _safe_print(text: str, **kwargs):
    """安全打印，处理 Windows GBK 编码问题"""
    try:
        _console.print(text, **kwargs)
    except Exception:
        # 回退：移除无法显示的字符
        safe_text = text.encode('gbk', errors='replace').decode('gbk', errors='replace')
        _console.print(safe_text, **kwargs)


def print_banner():
    _console.print(
        Panel.fit(
            "[bold cyan]XMclaw[/bold cyan] - Local-first AI Agent\n"
            "Type your message and press Enter. Use /quit to exit.",
            title="Welcome",
            border_style="cyan",
        )
    )


def print_user(text: str):
    _console.print(f"[bold green]You:[/bold green] {text}")


def print_agent(text: str, end: str = "\n", flush: bool = False):
    try:
        _console.print(text, end=end)
    except Exception:
        # 回退：移除 emoji 等无法显示的字符
        safe_text = text.encode('gbk', errors='replace').decode('gbk', errors='replace')
        _console.print(safe_text, end=end)


def print_tool(name: str, result: str):
    _console.print(
        Panel(
            f"[dim]{result[:500]}[/dim]",
            title=f"[yellow]Tool: {name}[/yellow]",
            border_style="yellow",
        )
    )


def print_state(state: str, thought: str):
    _safe_print(
        f"[dim italic]State: {state} | {thought[:100]}...[/dim italic]",
        highlight=False,
    )


def print_ask_user(question: str):
    _safe_print(
        Panel(
            f"[bold magenta]{question}[/bold magenta]",
            title="[magenta]XMclaw 询问[/magenta]",
            border_style="magenta",
        )
    )


def print_reflection(data: dict):
    summary = data.get("summary", "")
    problems = data.get("problems", [])
    lessons = data.get("lessons", [])
    improvements = data.get("improvements", [])
    content = f"[bold]{summary}[/bold]\n\n"
    if problems:
        content += "[red]问题:[/red]\n" + "\n".join(f"  - {p}" for p in problems) + "\n\n"
    if lessons:
        content += "[yellow]教训:[/yellow]\n" + "\n".join(f"  - {l}" for l in lessons) + "\n\n"
    if improvements:
        content += "[green]改进:[/green]\n" + "\n".join(f"  - {i}" for i in improvements) + "\n\n"
    _safe_print(
        Panel(content.strip(), title="[cyan]Reflection[/cyan]", border_style="cyan")
    )


def create_spinner(text: str = "Thinking..."):
    return Spinner("dots", text=text)
