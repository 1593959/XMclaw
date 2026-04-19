"""Path management utilities."""
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent


def get_agent_dir(agent_id: str) -> Path:
    return BASE_DIR / "agents" / agent_id


def get_shared_dir() -> Path:
    return BASE_DIR / "shared"


def get_logs_dir() -> Path:
    return BASE_DIR / "logs"


def get_tmp_dir() -> Path:
    return BASE_DIR / "tmp"


def get_cache_dir() -> Path:
    return BASE_DIR / "cache"
