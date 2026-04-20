"""Tests for DaemonConfig loading, env overrides, and secret masking."""
import json
import os
import tempfile
from pathlib import Path

import pytest
from xmclaw.daemon.config import _infer_type


class TestDaemonConfigDefault:
    """Tests for DaemonConfig.default()."""

    def test_default_has_all_sections(self):
        from xmclaw.daemon.config import DaemonConfig
        cfg = DaemonConfig.default()
        for section in ("llm", "evolution", "memory", "tools", "gateway", "mcp_servers", "integrations"):
            assert hasattr(cfg, section), f"Missing section: {section}"
        assert cfg.llm["default_provider"] == "anthropic"
        assert cfg.evolution["vfm_threshold"] == 5.0
        assert cfg.gateway["port"] == 8765

    def test_default_api_keys_are_empty(self):
        from xmclaw.daemon.config import DaemonConfig
        cfg = DaemonConfig.default()
        assert cfg.llm["openai"]["api_key"] == ""
        assert cfg.llm["anthropic"]["api_key"] == ""


class TestEnvOverrides:
    """Tests for XMC__ environment variable overrides."""

    def test_simple_env_override(self):
        """XMC__evolution__enabled=false should override the config."""
        with tempfile.TemporaryDirectory() as tmpdir:
            from xmclaw.daemon.config import DaemonConfig
            cfg_path = Path(tmpdir) / "config.json"
            # Write a known config
            cfg_data = DaemonConfig.default().__dict__
            with open(cfg_path, "w") as f:
                json.dump(cfg_data, f)
            # Set env var
            os.environ["XMC__evolution__enabled"] = "false"
            try:
                cfg = DaemonConfig.load(cfg_path)
                assert cfg.evolution["enabled"] is False
            finally:
                del os.environ["XMC__evolution__enabled"]

    def test_nested_env_override_int(self):
        """Nested int values should be parsed from string."""
        with tempfile.TemporaryDirectory() as tmpdir:
            from xmclaw.daemon.config import DaemonConfig
            cfg_path = Path(tmpdir) / "config.json"
            cfg_data = DaemonConfig.default().__dict__
            with open(cfg_path, "w") as f:
                json.dump(cfg_data, f)
            os.environ["XMC__gateway__port"] = "9999"
            try:
                cfg = DaemonConfig.load(cfg_path)
                assert cfg.gateway["port"] == 9999
                assert isinstance(cfg.gateway["port"], int)
            finally:
                del os.environ["XMC__gateway__port"]

    def test_env_override_not_set_leaves_file_value(self):
        """When no env var is set, file value is used."""
        with tempfile.TemporaryDirectory() as tmpdir:
            from xmclaw.daemon.config import DaemonConfig
            cfg_path = Path(tmpdir) / "config.json"
            cfg_data = DaemonConfig.default().__dict__
            cfg_data["evolution"]["interval_minutes"] = 60
            with open(cfg_path, "w") as f:
                json.dump(cfg_data, f)
            # Ensure no env var is leaking
            for k in list(os.environ):
                if k.startswith("XMC__"):
                    del os.environ[k]
            cfg = DaemonConfig.load(cfg_path)
            assert cfg.evolution["interval_minutes"] == 60


class TestMaskSecrets:
    """Tests for DaemonConfig.mask_secrets()."""

    def test_api_key_masked(self):
        from xmclaw.daemon.config import DaemonConfig
        cfg = DaemonConfig.default()
        cfg.llm["anthropic"]["api_key"] = "sk-ant-secret123"
        safe = cfg.mask_secrets()
        assert safe.llm["anthropic"]["api_key"] == "***"

    def test_bot_token_masked(self):
        from xmclaw.daemon.config import DaemonConfig
        cfg = DaemonConfig.default()
        cfg.integrations["slack"]["bot_token"] = "xoxb-real-token"
        safe = cfg.mask_secrets()
        assert safe.integrations["slack"]["bot_token"] == "***"

    def test_non_secret_not_masked(self):
        from xmclaw.daemon.config import DaemonConfig
        cfg = DaemonConfig.default()
        cfg.evolution["interval_minutes"] = 60
        safe = cfg.mask_secrets()
        assert safe.evolution["interval_minutes"] == 60

    def test_nested_secret_masked(self):
        from xmclaw.daemon.config import DaemonConfig
        cfg = DaemonConfig.default()
        cfg.integrations["github"]["token"] = "ghp_secret"
        safe = cfg.mask_secrets()
        assert safe.integrations["github"]["token"] == "***"


class TestTypeInference:
    """Tests for _infer_type()."""

    def test_bool_true(self):
        for val in ("true", "True", "TRUE", "yes", "1", "on"):
            assert _infer_type(val) is True, f"Failed for {val}"

    def test_bool_false(self):
        for val in ("false", "False", "FALSE", "no", "0", "off"):
            assert _infer_type(val) is False, f"Failed for {val}"

    def test_int(self):
        assert _infer_type("42") == 42
        assert _infer_type("0") == 0

    def test_float(self):
        assert _infer_type("3.14") == 3.14
        assert _infer_type("0.5") == 0.5

    def test_string_fallback(self):
        assert _infer_type("hello world") == "hello world"
        assert _infer_type("sk-ant-abc123") == "sk-ant-abc123"
