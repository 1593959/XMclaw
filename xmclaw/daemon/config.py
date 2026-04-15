"""Configuration loading."""
import json
from pathlib import Path
from dataclasses import dataclass
from xmclaw.utils.paths import BASE_DIR


@dataclass
class DaemonConfig:
    llm: dict
    evolution: dict
    memory: dict
    tools: dict
    gateway: dict
    mcp_servers: dict

    @classmethod
    def load(cls, path: Path | None = None) -> "DaemonConfig":
        path = path or BASE_DIR / "daemon" / "config.json"
        if not path.exists():
            config = cls.default()
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(config.__dict__, f, indent=2, ensure_ascii=False)
            return config
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Backfill missing fields for compatibility
        default_data = cls.default().__dict__
        for key, value in default_data.items():
            if key not in data:
                data[key] = value
        return cls(**data)

    @classmethod
    def default(cls) -> "DaemonConfig":
        return cls(
            llm={
                "default_provider": "anthropic",
                "openai": {
                    "api_key": "",
                    "base_url": "https://api.openai.com/v1",
                    "default_model": "gpt-4.1",
                },
                "anthropic": {
                    "api_key": "",
                    "base_url": "https://api.anthropic.com",
                    "default_model": "claude-sonnet-4-6",
                },
            },
            evolution={
                "enabled": True,
                "interval_minutes": 30,
                "daily_review_hour": 22,
                "vfm_threshold": 20,
                "max_genes_per_day": 10,
                "auto_rollback": True,
            },
            memory={
                "vector_db_path": str(BASE_DIR / "shared" / "vector_db"),
                "session_retention_days": 7,
                "max_context_tokens": 120000,
            },
            tools={
                "bash_timeout": 300,
                "sandbox_timeout": 30,
                "browser_headless": False,
            },
            gateway={
                "host": "127.0.0.1",
                "port": 8765,
            },
            mcp_servers={},
        )
