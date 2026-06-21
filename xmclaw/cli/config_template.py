"""Shared default daemon/config.json template.

Single source of truth for both ``xmclaw config init`` and
:class:`xmclaw.cli.doctor_registry.ConfigCheck`'s auto-fix. Kept as a
Python literal (not a read from ``daemon/config.example.json``) so
the pip-installed wheel works the same as a source checkout.

This literal now includes **all** sections with their daemon-level
defaults.  Generated configs are fully populated (not a minimum-viable
skeleton) so operators see every knob, while every value still matches
the hard-coded defaults in factory.py and the consuming subsystems.
"""
from __future__ import annotations

from typing import Any


def default_config_template() -> dict[str, Any]:
    """Fresh dict on every call -- callers may mutate to inject api keys."""
    return {
        "llm": {
            "default_provider": "anthropic",
            "openai": {
                "api_key": "",
                "base_url": "https://api.openai.com/v1",
                "default_model": "",
                "context_length": 0
            },
            "anthropic": {
                "api_key": "",
                "base_url": "https://api.anthropic.com",
                "default_model": "",
                "context_length": 0
            },
            "openrouter": {
                "api_key": "",
                "base_url": "https://openrouter.ai/api/v1",
                "default_model": "anthropic/claude-sonnet-4"
            },
            "profiles": []
        },
        "skills": {
            "disclosure_mode": "auto",
            "unified_threshold": 20,
            "semantic_discovery": {
                "enabled": True,
                "floor": 0.3
            },
            "induction": {
                "enabled": True,
                "interval_s": 86400,
                "check_interval_s": 1800,
                "warmup_s": 600,
                "max_per_pass": 1,
                "announce": True
            }
        },
        "evolution": {
            "enabled": True,
            "auto_apply": True,
            "interval_minutes": 30,
            "daily_review_hour": 22,
            "vfm_threshold": 5.0,
            "max_genes_per_day": 10,
            "auto_rollback": True,
            "scheduler": {
                "idle_aware": True,
                "idle_short_s": 300,
                "idle_long_s": 1800,
                "poll_interval_s": 30
            },
            "dream": {
                "enabled": True,
                "hour": 3,
                "minute": 0,
                "daily_log_window_days": 7,
                "min_keep_ratio": 0.3
            },
            "memory": {
                "embedding": {
                    "provider": "openai",
                    "api_key": "",
                    "base_url": "http://127.0.0.1:11434/v1",
                    "model": "qwen3-embedding:0.6b",
                    "dimensions": 1024,
                    "max_batch_size": 16
                },
                "indexer": {
                    "poll_interval_s": 10
                },
                "workspace_paths": []
            }
        },
        "memory": {
            "enabled": True,
            "db_path": None,
            "embedding_dim": None,
            "ttl": {
                "short": 3600,
                "working": 86400,
                "long": None
            },
            "pinned_tags": [
                "identity",
                "user-profile"
            ],
            "retention": {
                "sweep_interval_s": 3600,
                "prune_by_ttl": True,
                "max_items": {
                    "short": 2000,
                    "working": 20000,
                    "long": None
                },
                "max_bytes": {
                    "short": 10485760,
                    "working": 104857600,
                    "long": None
                }
            }
        },
        "tools": {
            "allowed_dirs": [],
            "enable_bash": True,
            "enable_web": True,
            "enable_browser": True,
            "browser": {
                "allowed_hosts": None,
                "headless": True,
                "timeout_ms": 15000,
                "download_dir": None
            },
            "composio": {
                "enabled": True,
                "api_key": "",
                "entity_id": "default",
                "apps": [
                    "GMAIL",
                    "SLACK",
                    "GITHUB",
                    "NOTION"
                ],
                "cache_ttl_s": 300
            },
            "computer_use": {
                "enabled": True,
                "screenshot_dir": None,
                "base64_size_cap": 524288
            },
            "media": {
                "enabled": True,
                "media_dir": None,
                "base64_size_cap": 524288
            },
            "subagent_fanout": {
                "enabled": True,
                "max_concurrency": 4,
                "fanout_timeout_s": 300,
                "per_subagent_timeout_s": 300
            }
        },
        "voice": {
            "stt": {
                "model": "tiny",
                "device": "cpu",
                "compute_type": "int8",
                "language": None
            },
            "tts": {
                "voice": "zh-CN-XiaoxiaoNeural",
                "rate": "+0%",
                "volume": "+0%"
            }
        },
        "runtime": {
            "backend": "local",
            "docker": {
                "image": "python:3.10-slim",
                "network_mode": "none",
                "mem_limit": "512m",
                "cpu_quota": 50000,
                "cpu_period": 100000,
                "read_only": True,
                "tmpfs": {
                    "/tmp": "size=100M"
                },
                "timeout_s": 30
            }
        },
        "gateway": {
            "host": "127.0.0.1",
            "port": 8766
        },
        "agent": {
            "max_hops": 100
        },
        "security": {
            "prompt_injection": "detect_only",
            "guardians": {
                "enabled": False,
                "computer_use_mode": "allow",
                "sensitive_files": [
                    "~/.ssh",
                    "~/.gnupg",
                    "~/.aws",
                    "~/.xmclaw.secret"
                ],
                "policy": {
                    "critical": "deny",
                    "high": "approve",
                    "medium": "allow",
                    "low": "allow",
                    "info": "allow"
                }
            }
        },
        "backup": {
            "auto_daily": False,
            "interval_s": 86400,
            "keep": 7,
            "name_prefix": "auto-"
        },
        "mcp_servers": {},
        "cognition": {
            "memory": {
                "legacy_recall_enabled": False
            },
            "enabled": True,
            "memory_v2": {
                "enabled": True,
                "recall_top_k": 5,
                "reflection": {
                    "enabled": True,
                    "interval_minutes": 30,
                    "max_sessions_per_tick": 20,
                    "lookback_days": 14
                },
                "retention": {
                    "sweep_interval_s": 3600,
                    "ttl": {
                        "working": 86400,
                        "long_term": None
                    },
                    "max_items": {
                        "working": 20000,
                        "long_term": None
                    },
                    "max_bytes": {
                        "working": 104857600,
                        "long_term": None
                    },
                    "dedup_every_n_sweeps": 24,
                    "dedup_scopes": [
                        "user",
                        "project",
                        "session"
                    ],
                    "llm_dedup_every_n_sweeps": 0
                },
                "curator": {
                    "enabled": True,
                    "interval_s": 86400,
                    "check_interval_s": 1800,
                    "warmup_s": 180,
                    "time_budget_s": 30,
                    "scopes": [
                        "user",
                        "project",
                        "session"
                    ],
                    "do_dedup": True,
                    "do_prune": True,
                    "do_contradict": True,
                    "do_crystallize": True,
                    "announce": True
                },
                "write_decision": {
                    "enabled": True
                },
                "gateway": {
                    "enabled": True,
                    "think": {
                        "enabled": True,
                        "model_tier": "fast",
                        "max_observations_per_batch": 5,
                        "cache_ttl_s": 300
                    },
                    "decide": {
                        "enabled": True,
                        "use_remember_with_decision": True,
                        "max_neighbors": 16
                    },
                    "recall": {
                        "gate_enabled": True,
                        "classify_enabled": True,
                        "hybrid_enabled": True,
                        "timeout_s": 3.0,
                        "k": 4,
                        "min_similarity": 0.72
                    }
                }
            },
            "auto_recall": {
                "enabled": True,
                "k": 8,
                "min_similarity": 0.65,
                "use_hybrid": True,
                "timeout_s": 1.0,
                "exclude_buckets": []
            },
            "context_compression": {
                "threshold_percent": 0.85,
                "protect_first_n": 3,
                "protect_last_n": 5,
                "protect_last_ratio": 0.2,
                "summary_target_ratio": 0.3
            },
            "continuous_loop": {
                "enabled": True,
                "autonomy_level": 50,
                "heartbeat_hz": 1.0,
                "action_threshold": 0.6,
                "top_k_focus": 7,
                "max_pending_goals": 16
            },
            "proactive": {
                "enabled": True,
                "tick_interval_s": 30.0,
                "calendar_ics_path": "",
                "cron_jobs": [
                    {
                        "name": "morning_briefing",
                        "schedule": "0 8 * * *",
                        "message": "☀️ 早安。要不要把今天日历和未完任务过一下？",
                        "urgency": "normal",
                        "enabled": False
                    },
                    {
                        "name": "lunch_break",
                        "schedule": "0 12 * * MON-FRI",
                        "message": "🍱 该休息吃午饭了",
                        "urgency": "low",
                        "enabled": False
                    },
                    {
                        "name": "evening_review",
                        "schedule": "0 22 * * *",
                        "message": "🌙 今天搞了啥？要不要我帮你过一遍 Dashboard 时间线？",
                        "urgency": "normal",
                        "enabled": False
                    }
                ],
                "channel_push": {
                    "enabled": True,
                    "min_urgency": "normal"
                },
                "daily_digest": {
                    "enabled": True,
                    "schedule": "0 22 * * *",
                    "lookback_h": 24.0,
                    "urgency": "normal"
                },
                "intent_prediction_cooldown_s": 600,
                "intent_prediction_threshold": 0.6,
                "disabled_triggers": []
            }
        },
        "channels": {
            "feishu": {
                "enabled": False,
                "app_id": "",
                "app_secret": "",
                "encrypt_key": "",
                "verify_token": "",
                "proactive_chat_id": "",
                "session_per_user": False
            },
            "telegram": {
                "enabled": False,
                "bot_token": "",
                "allowed_user_ids": [],
                "allowed_chat_ids": [],
                "parse_mode": None,
                "injection_policy": "detect_only"
            },
            "slack": {
                "enabled": False,
                "bot_token": "",
                "app_token": "",
                "allowed_user_ids": [],
                "allowed_channel_ids": [],
                "dispatch_session_id_prefix": "slack-",
                "injection_policy": "detect_only"
            },
            "dingtalk": {
                "enabled": False,
                "client_id": "",
                "client_secret": "",
                "robot_code": "",
                "allowed_user_ids": [],
                "allowed_conversation_ids": [],
                "injection_policy": "detect_only"
            },
            "wecom": {
                "enabled": False,
                "webhook_url": "",
                "msgtype": "markdown",
                "mentioned_list": [],
                "mentioned_mobile_list": []
            },
            "email": {
                "enabled": False,
                "imap_host": "imap.gmail.com",
                "imap_port": 993,
                "imap_user": "",
                "imap_password": "",
                "imap_folder": "INBOX",
                "imap_processed_folder": "",
                "poll_interval_s": 30,
                "smtp_host": "smtp.gmail.com",
                "smtp_port": 465,
                "smtp_user": "",
                "smtp_password": "",
                "smtp_use_ssl": True,
                "from_address": "",
                "from_name": "XMclaw",
                "allowed_senders": [],
                "injection_policy": "detect_only"
            }
        },
        "integrations": {
            "slack": {
                "enabled": False,
                "bot_token": "",
                "app_token": "",
                "channel": ""
            },
            "discord": {
                "enabled": False,
                "bot_token": "",
                "channel_id": ""
            },
            "telegram": {
                "enabled": False,
                "bot_token": "",
                "chat_id": ""
            },
            "github": {
                "enabled": False,
                "token": "",
                "repo": "",
                "poll_interval": 60
            },
            "notion": {
                "enabled": False,
                "api_key": "",
                "database_id": ""
            },
            "email": {
                "smtp_host": "",
                "smtp_port": 587,
                "username": "",
                "password": "",
                "from": "",
                "use_tls": True
            },
            "feishu": {
                "webhook_url": "",
                "secret": ""
            },
            "wecom": {
                "webhook_url": ""
            },
            "dingtalk": {
                "webhook_url": "",
                "secret": ""
            },
            "qq": {
                "base_url": "",
                "access_token": ""
            }
        },
        "hooks": []
    }
