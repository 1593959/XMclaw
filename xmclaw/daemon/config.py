"""Configuration loading with environment variable override, secret encryption, and hot-reload."""
import asyncio
import base64
import hashlib
import json
import os
import platform
import secrets
import time
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Callable
from xmclaw.utils.paths import BASE_DIR

# Environment variable prefix for config overrides
ENV_PREFIX = "XMC_"

# Known secret field names (values are masked in logs/output)
_SECRET_KEYS = frozenset({
    "api_key", "bot_token", "app_token", "token", "secret",
    "password", "private_key", "access_token", "refresh_token",
})

# Encryption marker in config values
_ENC_PREFIX = "ENC:"

# Hot-reload debounce: ignore chganges within this window (seconds)
_RELOAD_DEBOUNCE = 2.0


# ── Secret encryption (Fernet symmetric) ──────────────────────────────────────

def _get_encryption_key() -> bytes:
    """Derive a stable machine-specific key from system identifiers.

    Key is deterministic for this machine — the same machine always derives
    the same key. Not cryptographically strong (PBKDF2 would be better) but
    sufficient for local storage of development API keys.

    Override via environment variable for stronger key management.
    """
    env_key = os.environ.get("XMC_SECRET_KEY")
    if env_key:
        return hashlib.sha256(env_key.encode()).digest()[:32]

    # Derive from machine fingerprint
    parts = [
        platform.node(),
        platform.machine(),
        str(Path.home()),
        str(BASE_DIR),
    ]
    fingerprint = "|".join(parts).encode()
    return hashlib.pbkdf2_hmac("sha256", fingerprint, b"xmclaw-salt-v1", 100_000, dklen=32)


def _encrypt_secret(plaintext: str) -> str:
    """Encrypt a plaintext string, returning an 'ENC:...' string."""
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        return plaintext  # Fallback: store unencrypted if cryptography not installed

    key = base64.urlsafe_b64encode(_get_encryption_key())
    f = Fernet(key)
    token = f.encrypt(plaintext.encode())
    return _ENC_PREFIX + base64.urlsafe_b64encode(token).decode()


def _decrypt_secret(ciphertext: str) -> str:
    """Decrypt an 'ENC:...' string back to plaintext."""
    if not ciphertext.startswith(_ENC_PREFIX):
        return ciphertext  # Not encrypted
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        return ciphertext  # Can't decrypt without cryptography

    try:
        key = base64.urlsafe_b64encode(_get_encryption_key())
        f = Fernet(key)
        raw = base64.urlsafe_b64decode(ciphertext[len(_ENC_PREFIX):])
        return f.decrypt(raw).decode()
    except Exception:
        # Key mismatch or corrupt data — return as-is (will fail at API call)
        return ciphertext


def _decrypt_secrets(obj):
    """Recursively decrypt all secret values in a config dict."""
    if isinstance(obj, dict):
        return {k: (_decrypt_secret(v) if k.lower() in _SECRET_KEYS and isinstance(v, str) else _decrypt_secrets(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decrypt_secrets(i) for i in obj]
    return obj


def encrypt_value(value: str) -> str:
    """Public helper: encrypt a plaintext string for config storage.

    Usage in config init wizard:
        encrypted = encrypt_value(user_input_key)
        config["llm"]["anthropic"]["api_key"] = encrypted
    """
    return _encrypt_secret(value)


def decrypt_value(value: str) -> str:
    """Public helper: decrypt an encrypted config value."""
    return _decrypt_secret(value)


# ── Env override helpers ───────────────────────────────────────────────────────

def _env_to_nested_key(env_name: str) -> tuple[str, str] | None:
    """Convert XMC_llm__openai__api_key → ('llm', 'openai.api_key').
    Also handles double-underscore: XMC__evolution__enabled → ('evolution', 'enabled').
    """
    if not env_name.startswith(ENV_PREFIX):
        return None
    rest = env_name[len(ENV_PREFIX):].lower()
    parts = rest.split("__")
    if len(parts) < 2:
        return None
    section = parts[0].lstrip("_")  # strip leading underscore from XMC__ → section case
    nested = ".".join(parts[1:])
    return (section, nested)


def _apply_env_override(data: dict) -> dict:
    """Scan env vars prefixed with XMC_ and override matching config keys."""
    overridden = []
    for env_name, env_val in os.environ.items():
        if not env_name.startswith(ENV_PREFIX):
            continue
        result = _env_to_nested_key(env_name)
        if result is None:
            continue
        section, key = result
        if section not in data:
            continue
        section_data = data[section]
        if not isinstance(section_data, dict):
            continue
        typed_val = _infer_type(env_val)
        # Support nested dot-notation: key="openai.api_key" → data[section][openai][api_key]
        parts = key.split(".")
        target = section_data
        for p in parts[:-1]:
            if p not in target:
                target[p] = {}
            target = target[p]
        target[parts[-1]] = typed_val
        overridden.append(f"{section}.{key}")
    if overridden:
        import structlog
        logger = structlog.get_logger()
        logger.info("config_env_overrides_applied", keys=overridden)
    return data


def _infer_type(val: str):
    """Try to parse string value as int/float/bool, else return as string."""
    if val.lower() in ("true", "yes", "1", "on"):
        return True
    if val.lower() in ("false", "no", "0", "off"):
        return False
    if val.isdigit():
        return int(val)
    try:
        return float(val)
    except ValueError:
        return val


@dataclass
class DaemonConfig:
    llm: dict
    evolution: dict
    memory: dict
    tools: dict
    gateway: dict
    mcp_servers: dict
    integrations: dict
    security: dict = field(default_factory=dict)  # Hot-reload & permissions config

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
        # Decrypt encrypted secrets (run after backfill so keys exist)
        data = _decrypt_secrets(data)
        # Apply environment variable overrides (env vars take final say)
        data = _apply_env_override(data)
        instance = cls(**data)
        instance._file_path = path
        return instance

    def mask_secrets(self) -> "DaemonConfig":
        """Return a DaemonConfig copy with secret values replaced by '***'."""
        def _mask_obj(obj):
            if isinstance(obj, dict):
                return {k: ("***" if k.lower() in _SECRET_KEYS and isinstance(v, str) else _mask_obj(v))
                        for k, v in obj.items()}
            if isinstance(obj, list):
                return [_mask_obj(i) for i in obj]
            return obj
        masked_data = _mask_obj(self.__dict__)
        return DaemonConfig(**masked_data)

    # ── Hot-reload support ─────────────────────────────────────────────────────

    _file_path: Path = field(default=None, repr=False)
    _last_mtime: float = field(default=0.0, repr=False)
    _watch_task: asyncio.Task | None = field(default=None, repr=False)
    _on_change_callbacks: list[Callable[["DaemonConfig"], None]] = field(
        default_factory=list, repr=False
    )

    async def start_watching(self, on_change: Callable[["DaemonConfig"], None] | None = None) -> None:
        """Start watching the config file for changes and reload on modify.

        Args:
            on_change: Optional callback called after each reload with the new config.
        """
        if self._file_path is None:
            return
        if on_change:
            self._on_change_callbacks.append(on_change)

        if self._watch_task is not None:
            return  # Already watching

        sec = self.security or {}
        if not sec.get("hot_reload", True):
            return

        self._last_mtime = self._file_path.stat().st_mtime if self._file_path.exists() else 0.0
        self._watch_task = asyncio.create_task(self._watch_loop())
        import structlog
        logger = structlog.get_logger()
        logger.info("config_hot_reload_started", path=str(self._file_path))

    async def _watch_loop(self) -> None:
        """Poll loop: re-check mtime every 5 seconds."""
        import structlog
        logger = structlog.get_logger()
        while True:
            await asyncio.sleep(5)
            try:
                if not self._file_path.exists():
                    continue
                mtime = self._file_path.stat().st_mtime
                if mtime != self._last_mtime:
                    # Debounce: wait a bit to catch rapid saves
                    await asyncio.sleep(_RELOAD_DEBOUNCE)
                    if self._file_path.stat().st_mtime != self._last_mtime:
                        self._last_mtime = self._file_path.stat().st_mtime
                        new_config = DaemonConfig.load(self._file_path)
                        # Copy fields over
                        self.llm = new_config.llm
                        self.evolution = new_config.evolution
                        self.memory = new_config.memory
                        self.tools = new_config.tools
                        self.security = new_config.security
                        self.gateway = new_config.gateway
                        self.mcp_servers = new_config.mcp_servers
                        self.integrations = new_config.integrations
                        logger.info("config_hot_reloaded", path=str(self._file_path))
                        for cb in self._on_change_callbacks:
                            try:
                                cb(self)
                            except Exception as e:
                                logger.warning("config_change_callback_failed", error=str(e))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("config_watch_error", error=str(e))

    def stop_watching(self) -> None:
        """Stop the hot-reload watcher."""
        if self._watch_task:
            self._watch_task.cancel()
            self._watch_task = None
            import structlog
            logger = structlog.get_logger()
            logger.info("config_hot_reload_stopped")

    @classmethod
    def default(cls) -> "DaemonConfig":
        return cls(
            llm={
                "default_provider": "anthropic",
                "openai": {
                    "api_key": "",
                    "base_url": "https://api.openai.com/v1",
                    "default_model": "",
                },
                "anthropic": {
                    "api_key": "",
                    "base_url": "https://api.anthropic.com",
                    "default_model": "",
                },
            },
            evolution={
                "enabled": True,
                "interval_minutes": 30,
                "daily_review_hour": 22,
                "vfm_threshold": 5.0,
                "max_genes_per_day": 10,
                "auto_rollback": True,
                # Auto-rollback thresholds (Phase E3):
                #   harmful_count_threshold — a promoted skill is retired once
                #     harmful_count reaches this value AND strictly exceeds
                #     helpful_count. Set ≥2 to tolerate transient tool errors.
                #   harmful_ratio_threshold — additional guard: harmful_count /
                #     max(matched_count,1) above this ratio also trips rollback,
                #     catching slow-burn regressions where a skill is wrong
                #     more often than right but never hits the absolute count.
                "rollback_harmful_count_threshold": 3,
                "rollback_harmful_ratio_threshold": 0.5,
                "rollback_min_matches": 4,
                # Pattern detection thresholds
                "pattern_thresholds": {
                    "tool_usage_min_count": 2,    # 工具使用次数阈值 (达到此次数触发模式检测)
                    "repeated_request_min_count": 2,  # 重复请求次数阈值
                    "insight_tool_usage_count": 2,    # 洞察提取的最小工具使用次数
                    "insight_repeated_count": 2,      # 洞察提取的最小重复次数
                },
                # 工具特定阈值 (可针对不同工具设置不同阈值)
                "tool_specific_thresholds": {
                    "web_search": 3,     # 搜索频繁，允许更高阈值
                    "file_write": 2,     # 文件操作应更敏感
                    "bash": 2,           # 命令执行应更敏感
                    "web_fetch": 3,      # 网页获取可稍高
                    "code_exec": 2,      # 代码执行应敏感
                },
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
            security={
                "enabled": True,
                "hot_reload": True,
                "tool_permissions": {},  # tool_name -> "allow"|"ask"|"block"
                "network_allowed_patterns": [
                    r"^https?://",
                    r"^ws[s]?://",
                ],
                "allowed_paths": [],  # empty = BASE_DIR only
            },
            gateway={
                "host": "127.0.0.1",
                "port": 8765,
            },
            mcp_servers={},
            integrations={
                "slack":    {"enabled": False, "bot_token": "", "app_token": "", "channel": ""},
                "discord":  {"enabled": False, "bot_token": "", "channel_id": ""},
                "telegram": {"enabled": False, "bot_token": "", "chat_id": ""},
                "github":   {"enabled": False, "token": "", "repo": "", "poll_interval": 60},
                "notion":   {"enabled": False, "api_key": "", "database_id": ""},
                "feishu": {
                    "enabled": False,
                    "app_id": "",
                    "app_secret": "",
                    "bot_name": "XMclaw",
                    "default_chat_id": "",
                },
                "qq": {
                    "enabled": False,
                    "mode": "websocket",   # "websocket" or "webhook"
                    "app_id": "",
                    "app_token": "",
                    "secret": "",
                    "webhook_url": "",
                    "webhook_token": "",
                    "guild_id": "",
                    "channel_id": "",
                },
                "wechat": {
                    "enabled": False,
                    "mode": "group_bot",   # "group_bot" or "application"
                    "webhook_url": "",
                    "corp_id": "",
                    "agent_id": "",
                    "app_secret": "",
                    "callback_token": "",
                    "callback_aes_key": "",
                    "default_to_user": "",
                },
            },
        )
