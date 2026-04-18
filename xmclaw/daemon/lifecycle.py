"""Daemon lifecycle management."""
import json
import os
import sys
import subprocess
from pathlib import Path
from xmclaw.utils.paths import BASE_DIR

PID_FILE = BASE_DIR / "daemon" / "pid"


def is_running() -> bool:
    if not PID_FILE.exists():
        return False
    try:
        pid = int(PID_FILE.read_text().strip())
        if sys.platform == "win32":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(1, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        else:
            os.kill(pid, 0)
            return True
    except (OSError, ValueError):
        return False


def _is_interactive() -> bool:
    """Return True if stdin is a TTY (interactive terminal)."""
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


def _prompt_api_key(provider: str, default_url: str) -> tuple[str, str]:
    """Prompt for API key. Returns (api_key, base_url)."""
    try:
        from rich.console import Console
        from rich.prompt import Prompt, Confirm
        console = Console()
        console.print(f"\n[bold cyan]🔑 {provider} 配置[/bold cyan]")
        api_key = Prompt.ask(f"  {provider.capitalize()} API Key", password=True)
        if not api_key.strip():
            console.print(f"  [yellow]跳过，将使用空 API Key[/yellow]")
            return ("", default_url)
        use_custom = Confirm.ask("  使用自定义 API 地址？", default=False)
        if use_custom:
            base_url = Prompt.ask("  API Base URL", default=default_url)
        else:
            base_url = default_url
        return (api_key.strip(), base_url.strip())
    except Exception:
        # Fallback to plain input if rich is not available
        print(f"\n--- {provider} 配置 ---")
        api_key = input(f"  {provider.capitalize()} API Key (回车跳过): ").strip()
        base_url = default_url
        return (api_key, base_url)


def run_setup_wizard() -> None:
    """Interactive first-run setup: collect API keys and write config."""
    try:
        from rich.console import Console
        from rich.panel import Panel
        console = Console()
    except Exception:
        console = None

    msg = (
        "XMclaw 首次运行配置向导\n\n"
        "请提供 LLM API Key（留空跳过，该 provider 将不可用）\n"
        "支持 OpenAI (GPT系列) 和 Anthropic (Claude系列)"
    )
    if console:
        console.print(Panel(msg, title="🛠️ XMclaw 首次运行", border_style="cyan"))
    else:
        print(msg)

    config_path = BASE_DIR / "daemon" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    # Load defaults from example (or DaemonConfig)
    try:
        from xmclaw.daemon.config import DaemonConfig
        default_cfg = DaemonConfig.default().__dict__
    except Exception:
        # Hardcoded fallback if import fails
        default_cfg = {
            "llm": {
                "default_provider": "anthropic",
                "openai": {"api_key": "", "base_url": "https://api.openai.com/v1", "default_model": ""},
                "anthropic": {"api_key": "", "base_url": "https://api.anthropic.com", "default_model": ""},
            },
            "evolution": {"enabled": True, "interval_minutes": 30, "daily_review_hour": 22,
                          "vfm_threshold": 5.0, "max_genes_per_day": 10, "auto_rollback": True},
            "memory": {"vector_db_path": str(BASE_DIR / "shared" / "vector_db"),
                       "session_retention_days": 7, "max_context_tokens": 120000},
            "tools": {"bash_timeout": 300, "sandbox_timeout": 30, "browser_headless": False},
            "gateway": {"host": "127.0.0.1", "port": 8765},
            "mcp_servers": {},
            "integrations": {
                "slack": {"enabled": False, "bot_token": "", "app_token": "", "channel": ""},
                "discord": {"enabled": False, "bot_token": "", "channel_id": ""},
                "telegram": {"enabled": False, "bot_token": "", "chat_id": ""},
                "github": {"enabled": False, "token": "", "repo": "", "poll_interval": 60},
                "notion": {"enabled": False, "api_key": "", "database_id": ""},
            },
        }

    # Prompt for keys
    anthropic_key, anthropic_url = _prompt_api_key(
        "anthropic", "https://api.anthropic.com")
    openai_key, openai_url = _prompt_api_key(
        "openai", "https://api.openai.com/v1")

    # Encrypt secrets before storing
    try:
        from xmclaw.daemon.config import encrypt_value
        _enc = encrypt_value
    except Exception:
        _enc = lambda v: v  # fallback: store plaintext if encryption unavailable

    default_cfg["llm"]["anthropic"]["api_key"] = _enc(anthropic_key) if anthropic_key else ""
    default_cfg["llm"]["anthropic"]["base_url"] = anthropic_url
    default_cfg["llm"]["openai"]["api_key"] = _enc(openai_key) if openai_key else ""
    default_cfg["llm"]["openai"]["base_url"] = openai_url

    # Choose default provider
    default_provider = "anthropic"
    if not anthropic_key and openai_key:
        default_provider = "openai"
    default_cfg["llm"]["default_provider"] = default_provider

    # Write config
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(default_cfg, f, indent=2, ensure_ascii=False)

    if console:
        console.print(f"\n[green]✅ 配置文件已保存至 {config_path}[/green]")
        console.print(f"  默认 Provider: [bold]{default_provider}[/bold]")
        if anthropic_key:
            console.print(f"  [green]✓[/green] Anthropic: 已配置")
        else:
            console.print(f"  [dim]  Anthropic: 未配置[/dim]")
        if openai_key:
            console.print(f"  [green]✓[/green] OpenAI: 已配置")
        else:
            console.print(f"  [dim]  OpenAI: 未配置[/dim]")
    else:
        print(f"\n✅ 配置文件已保存至 {config_path}")
        print(f"   默认 Provider: {default_provider}")
        print(f"   Anthropic: {'已配置' if anthropic_key else '未配置'}")
        print(f"   OpenAI: {'已配置' if openai_key else '未配置'}")


def start_daemon() -> int:
    if is_running():
        print("Daemon already running.")
        return 1

    config_path = BASE_DIR / "daemon" / "config.json"
    if not config_path.exists():
        if _is_interactive():
            print("\n⚠️  配置文件不存在，将进入首次运行配置向导...")
            run_setup_wizard()
        else:
            # Non-interactive: auto-create config with empty keys
            print("⚠️  config.json not found, creating with defaults (no API keys).")
            try:
                from xmclaw.daemon.config import DaemonConfig
                cfg = DaemonConfig.default()
                config_path.parent.mkdir(parents=True, exist_ok=True)
                with open(config_path, "w", encoding="utf-8") as f:
                    json.dump(cfg.__dict__, f, indent=2, ensure_ascii=False)
            except Exception:
                pass

    if sys.platform == "win32":
        # Use the project's own .venv, not any external Python installation.
        # BASE_DIR is C:\Users\15978\Desktop\XMclaw, so .venv is always alongside it.
        venv_python = BASE_DIR / ".venv" / "Scripts" / "python.exe"
        if venv_python.exists():
            python_exe = str(venv_python)
        else:
            python_exe = sys.executable
        proc = subprocess.Popen(
            [python_exe, "-m", "xmclaw.daemon.server"],
            creationflags=subprocess.CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        PID_FILE.write_text(str(proc.pid))
        print(f"Daemon started with PID {proc.pid}")
        return 0
    else:
        print("Daemon start on non-Windows not yet implemented.")
        return 1


def stop_daemon() -> int:
    if not is_running():
        print("Daemon not running.")
        return 1
    try:
        pid = int(PID_FILE.read_text().strip())
        if sys.platform == "win32":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            PROCESS_TERMINATE = 1
            handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
            if handle:
                kernel32.TerminateProcess(handle, 0)
                kernel32.CloseHandle(handle)
        else:
            os.kill(pid, 9)
        PID_FILE.unlink()
        print(f"Daemon stopped (PID {pid})")
        return 0
    except Exception as e:
        print(f"Failed to stop daemon: {e}")
        return 1


def daemon_status() -> int:
    if is_running():
        pid = int(PID_FILE.read_text().strip())
        print(f"Daemon is running (PID {pid})")
        return 0
    else:
        print("Daemon is not running.")
        return 1
