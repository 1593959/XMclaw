"""Onboarding wizard tests — Epic #9."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from xmclaw.cli.onboard import (
    OnboardAbort,
    _ask_api_key,
    _ask_overwrite,
    _ask_tools,
    _ask_workspace,
    _choose_provider,
    _smoke_test,
    run_onboard,
)


class TestAskOverwrite:
    def test_missing_file_returns_true(self, tmp_path: Path) -> None:
        assert _ask_overwrite(tmp_path / "no_such.json") is True

    def test_user_confirms_overwrite(self, tmp_path: Path) -> None:
        existing = tmp_path / "config.json"
        existing.write_text("{}")
        with patch("xmclaw.cli.onboard.questionary.confirm") as m:
            m.return_value.ask.return_value = True
            assert _ask_overwrite(existing) is True

    def test_user_denies_overwrite(self, tmp_path: Path) -> None:
        existing = tmp_path / "config.json"
        existing.write_text("{}")
        with patch("xmclaw.cli.onboard.questionary.confirm") as m:
            m.return_value.ask.return_value = False
            assert _ask_overwrite(existing) is False

    def test_none_raises_abort(self, tmp_path: Path) -> None:
        existing = tmp_path / "config.json"
        existing.write_text("{}")
        with patch("xmclaw.cli.onboard.questionary.confirm") as m:
            m.return_value.ask.return_value = None
            with pytest.raises(OnboardAbort):
                _ask_overwrite(existing)


class TestChooseProvider:
    def test_anthropic(self) -> None:
        with patch("xmclaw.cli.onboard.questionary.select") as m:
            m.return_value.ask.return_value = "anthropic"
            assert _choose_provider() == "anthropic"

    def test_openai(self) -> None:
        with patch("xmclaw.cli.onboard.questionary.select") as m:
            m.return_value.ask.return_value = "openai"
            assert _choose_provider() == "openai"

    def test_none_raises_abort(self) -> None:
        with patch("xmclaw.cli.onboard.questionary.select") as m:
            m.return_value.ask.return_value = None
            with pytest.raises(OnboardAbort):
                _choose_provider()


class TestAskApiKey:
    def test_returns_stripped_key(self) -> None:
        with patch("xmclaw.cli.onboard.questionary.password") as m:
            m.return_value.ask.return_value = " sk-ant-123 "
            assert _ask_api_key() == "sk-ant-123"

    def test_none_raises_abort(self) -> None:
        with patch("xmclaw.cli.onboard.questionary.password") as m:
            m.return_value.ask.return_value = None
            with pytest.raises(OnboardAbort):
                _ask_api_key()


class TestAskWorkspace:
    def test_defaults_to_data_dir(self) -> None:
        with patch("xmclaw.cli.onboard.questionary.text") as m:
            m.return_value.ask.return_value = str(Path.home() / ".xmclaw")
            result = _ask_workspace()
            assert result.name == ".xmclaw"

    def test_custom_path(self) -> None:
        with patch("xmclaw.cli.onboard.questionary.text") as m:
            m.return_value.ask.return_value = "/tmp/xmclaw-test"
            result = _ask_workspace()
            assert result == Path("/tmp/xmclaw-test")

    def test_expands_tilde(self) -> None:
        with patch("xmclaw.cli.onboard.questionary.text") as m:
            m.return_value.ask.return_value = "~/custom"
            result = _ask_workspace()
            assert result == Path.home() / "custom"

    def test_none_raises_abort(self) -> None:
        with patch("xmclaw.cli.onboard.questionary.text") as m:
            m.return_value.ask.return_value = None
            with pytest.raises(OnboardAbort):
                _ask_workspace()


class TestAskTools:
    def test_defaults_checked(self) -> None:
        with patch("xmclaw.cli.onboard.questionary.checkbox") as m:
            m.return_value.ask.return_value = ["bash", "web"]
            result = _ask_tools()
            assert result["enable_bash"] is True
            assert result["enable_web"] is True
            assert result["enable_browser"] is False

    def test_browser_enabled(self) -> None:
        with patch("xmclaw.cli.onboard.questionary.checkbox") as m:
            m.return_value.ask.return_value = ["bash", "web", "browser"]
            result = _ask_tools()
            assert result["enable_browser"] is True

    def test_none_raises_abort(self) -> None:
        with patch("xmclaw.cli.onboard.questionary.checkbox") as m:
            m.return_value.ask.return_value = None
            with pytest.raises(OnboardAbort):
                _ask_tools()


class TestSmokeTest:
    def test_unknown_provider_skips(self) -> None:
        ok, err = _smoke_test("ollama")
        assert ok is True
        assert "skipping" in err.lower()

    def test_anthropic_reachable(self) -> None:
        """Mock a 401 — endpoint reachable, auth missing (expected)."""
        from urllib.error import HTTPError

        with patch("urllib.request.urlopen") as m:
            m.side_effect = HTTPError(
                url="https://api.anthropic.com/v1/health",
                code=401,
                msg="Unauthorized",
                hdrs={},
                fp=None,
            )
            ok, err = _smoke_test("anthropic")
            assert ok is True
            assert err == ""

    def test_unreachable(self) -> None:
        with patch("urllib.request.urlopen") as m:
            m.side_effect = OSError("network down")
            ok, err = _smoke_test("openai")
            assert ok is False
            assert "network down" in err


class TestRunOnboard:
    def test_happy_path_writes_config_and_secret(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cfg_path = tmp_path / "config.json"
        # Phase 2: onboard calls set_secret(...) with the default
        # ``backend="encrypted"``, so pin both stores under tmp_path and
        # round-trip the value via get_secret rather than grepping the
        # (no-longer-written) plaintext secrets.json.
        monkeypatch.setenv("XMC_SECRETS_PATH", str(tmp_path / "secrets.json"))
        monkeypatch.setenv("XMC_SECRET_DIR", str(tmp_path / ".xmclaw.secret"))
        monkeypatch.setenv("XMC_DATA_DIR", str(tmp_path / "workspace"))

        with patch("xmclaw.cli.onboard.questionary.confirm") as m_confirm, \
             patch("xmclaw.cli.onboard.questionary.select") as m_select, \
             patch("xmclaw.cli.onboard.questionary.password") as m_password, \
             patch("xmclaw.cli.onboard.questionary.text") as m_text, \
             patch("xmclaw.cli.onboard.questionary.checkbox") as m_checkbox, \
             patch("xmclaw.cli.onboard._smoke_test") as m_smoke:

            m_confirm.return_value.ask.return_value = True
            m_select.return_value.ask.return_value = "anthropic"
            m_password.return_value.ask.return_value = "sk-ant-test"
            m_text.return_value.ask.return_value = str(tmp_path / "workspace")
            m_checkbox.return_value.ask.return_value = ["bash", "web"]
            m_smoke.return_value = (True, "")

            code = run_onboard(config_path=str(cfg_path), skip_smoke=False)
            assert code == 0

        # Config written
        assert cfg_path.exists()
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert cfg["llm"]["default_provider"] == "anthropic"
        assert cfg["tools"]["enable_bash"] is True
        assert cfg["tools"]["enable_web"] is True
        assert cfg["tools"]["enable_browser"] is False

        # Secret stored — resolve via get_secret, not direct plaintext
        # file inspection (Phase 2 writes to the encrypted store by
        # default so the plaintext file is not created).
        from xmclaw.utils.secrets import get_secret
        assert get_secret("llm.anthropic.api_key") == "sk-ant-test"

    def test_user_denies_overwrite_returns_zero(
        self, tmp_path: Path,
    ) -> None:
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text("{}")

        with patch("xmclaw.cli.onboard.questionary.confirm") as m_confirm:
            m_confirm.return_value.ask.return_value = False
            code = run_onboard(config_path=str(cfg_path))
            assert code == 0

    def test_smoke_failure_returns_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cfg_path = tmp_path / "config.json"
        monkeypatch.setenv("XMC_DATA_DIR", str(tmp_path / "workspace"))

        with patch("xmclaw.cli.onboard.questionary.confirm") as m_confirm, \
             patch("xmclaw.cli.onboard.questionary.select") as m_select, \
             patch("xmclaw.cli.onboard.questionary.password") as m_password, \
             patch("xmclaw.cli.onboard.questionary.text") as m_text, \
             patch("xmclaw.cli.onboard.questionary.checkbox") as m_checkbox, \
             patch("xmclaw.cli.onboard._smoke_test") as m_smoke:

            m_confirm.return_value.ask.return_value = True
            m_select.return_value.ask.return_value = "openai"
            m_password.return_value.ask.return_value = "sk-test"
            m_text.return_value.ask.return_value = str(tmp_path / "workspace")
            m_checkbox.return_value.ask.return_value = ["bash"]
            m_smoke.return_value = (False, "timeout")

            code = run_onboard(config_path=str(cfg_path), skip_smoke=False)
            assert code == 1

    def test_skip_smoke_omits_connectivity_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cfg_path = tmp_path / "config.json"
        monkeypatch.setenv("XMC_DATA_DIR", str(tmp_path / "workspace"))

        with patch("xmclaw.cli.onboard.questionary.confirm") as m_confirm, \
             patch("xmclaw.cli.onboard.questionary.select") as m_select, \
             patch("xmclaw.cli.onboard.questionary.password") as m_password, \
             patch("xmclaw.cli.onboard.questionary.text") as m_text, \
             patch("xmclaw.cli.onboard.questionary.checkbox") as m_checkbox, \
             patch("xmclaw.cli.onboard._smoke_test") as m_smoke:

            m_confirm.return_value.ask.return_value = True
            m_select.return_value.ask.return_value = "anthropic"
            m_password.return_value.ask.return_value = ""
            m_text.return_value.ask.return_value = str(tmp_path / "workspace")
            m_checkbox.return_value.ask.return_value = ["bash", "web"]

            code = run_onboard(config_path=str(cfg_path), skip_smoke=True)
            assert code == 0
            m_smoke.assert_not_called()
