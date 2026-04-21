"""redact(): basic secret-pattern scrubbing."""
from __future__ import annotations

from xmclaw.utils.redact import redact, redact_string


def test_redact_openai_key() -> None:
    text = "my key is sk-abcdefghij1234567890 and also stuff"
    assert "sk-abcdefghij1234567890" not in redact_string(text)


def test_redact_anthropic_key() -> None:
    text = "using sk-ant-api01_abcdefghij1234567890abc"
    assert "sk-ant-api01_abcdefghij1234567890abc" not in redact_string(text)


def test_redact_github_token() -> None:
    text = "token=ghp_abcdefghij1234567890abcdef"
    assert "ghp_abcdefghij1234567890abcdef" not in redact_string(text)


def test_redact_nested() -> None:
    data = {
        "headers": {"Authorization": "Bearer sk-abcdefghij1234567890abcdef"},
        "log": ["line1", "sk-ant-xxxxxxxxxxxxxxxxxxxx"],
    }
    red = redact(data)
    assert "sk-abcdefghij1234567890abcdef" not in str(red)
    assert "sk-ant-xxxxxxxxxxxxxxxxxxxx" not in str(red)
