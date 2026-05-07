"""B-246: pin redact pattern catalogue (12 new classes).

Pre-B-246 only 5 patterns were checked; AWS / Stripe / Anthropic admin
/ OpenRouter / DeepSeek / Discord / PEM private keys / JWTs all leaked
into events.db verbatim. These tests assert that each new pattern
fires + the original 5 still fire + non-secrets pass through.

NOTE: every test fixture is BUILT BY CONCATENATION at runtime instead
of being a literal string in this file. That keeps GitHub Push
Protection / TruffleHog / GitGuardian from flagging the test source
as a real leak — they pattern-match the literal token shape, so
``"AKIA" + "X"*16`` reads the SAME at runtime but doesn't trip the
scanner because the literal ``"AKIA..."`` never appears here.
"""
from __future__ import annotations

from xmclaw.utils.redact import redact, redact_string


# Helpers — concat at runtime so test source doesn't carry literal tokens.
def _mk(prefix: str, *, body_len: int = 32, alphabet: str = "X") -> str:
    return prefix + (alphabet * body_len)[:body_len]


# ── Original 5 (regression check) ──────────────────────────────────


def test_openai_sk_redacted() -> None:
    token = _mk("sk-", body_len=30)
    assert "sk-***" in redact_string(f"token={token}")


def test_anthropic_sk_ant_redacted() -> None:
    token = _mk("sk-ant-", body_len=30)
    out = redact_string(f"ANTHROPIC_API_KEY={token}")
    assert "sk-ant-***" in out


def test_slack_token_redacted() -> None:
    token = "xoxb-" + "1" * 5 + "-" + "2" * 4 + "-" + "X" * 12
    out = redact_string(f"Bearer {token}")
    assert "xox*-***" in out


def test_github_token_redacted() -> None:
    token = _mk("ghp_", body_len=36)
    out = redact_string(f"git push https://{token}@example.com")
    assert "gh*_***" in out


def test_google_aiza_redacted() -> None:
    token = _mk("AIza", body_len=35, alphabet="X")
    out = redact_string(f"GOOGLE_API_KEY={token}")
    assert "AIza***" in out


# ── B-246 new patterns ─────────────────────────────────────────────


def test_anthropic_admin_redacted_distinctly() -> None:
    """sk-ant-admin-... gets its own placeholder so audit logs show the
    operator-tier key was leaked (different blast radius than user tier)."""
    token = _mk("sk-ant-admin-", body_len=30)
    out = redact_string(f"export ADMIN={token}")
    assert "sk-ant-admin-***" in out
    assert "sk-ant-***" not in out  # generic rule didn't pre-empt


def test_openrouter_redacted_distinctly() -> None:
    token = _mk("sk-or-v1-", body_len=40)
    out = redact_string(f"OR_KEY={token}")
    assert "sk-or-***" in out


def test_deepseek_redacted_distinctly() -> None:
    token = _mk("sk-ds-", body_len=24)
    out = redact_string(f"DSK={token}")
    assert "sk-ds-***" in out


def test_openai_org_id_redacted() -> None:
    org = _mk("org-", body_len=24)
    out = redact_string(f"OpenAI-Organization: {org}")
    assert "org-***" in out


def test_aws_access_key_redacted() -> None:
    """AWS Access Key ID has fixed prefix AKIA + 16 alphanumerics."""
    # Construct so the literal "AKIA<16-chars>" doesn't appear in source.
    token = "AKIA" + "X" * 16
    out = redact_string(f"AWS_ACCESS_KEY_ID={token}")
    assert "AKIA***" in out


def test_stripe_keys_redacted() -> None:
    """sk_live_, sk_test_, pk_live_, rk_live_ all caught."""
    body = "X" * 24
    cases = [
        f"key={'sk' + '_live_' + body}",
        f"test={'sk' + '_test_' + body}",
        f"pub={'pk' + '_live_' + body}",
        f"rkey={'rk' + '_live_' + body}",
    ]
    for c in cases:
        out = redact_string(c)
        assert "stripe_***" in out, f"Stripe key not redacted in: {c}"


def test_discord_bot_token_redacted() -> None:
    """3-segment dot-format bot token."""
    seg1 = "M" + "X" * 23
    seg2 = "Y" * 6
    seg3 = "Z" * 27
    token = f"{seg1}.{seg2}.{seg3}"
    out = redact_string(f"Authorization: Bot {token}")
    assert "discord_***" in out


def test_pem_private_key_block_redacted() -> None:
    """Multi-line PEM private key replaced with placeholder."""
    begin = "-" * 5 + "BEGIN RSA PRIVATE KEY" + "-" * 5
    end = "-" * 5 + "END RSA PRIVATE KEY" + "-" * 5
    pem = f"{begin}\n{'X' * 50}\n{'Y' * 30}\n{end}"
    out = redact_string(f"key:\n{pem}\nthat's it")
    assert "[PEM_PRIVATE_KEY_REDACTED]" in out
    assert "BEGIN RSA PRIVATE KEY" not in out


def test_pem_generic_private_key_redacted() -> None:
    """``-----BEGIN PRIVATE KEY-----`` (no algorithm prefix, e.g. PKCS8
    / GCP service account JSON inline)."""
    begin = "-" * 5 + "BEGIN PRIVATE KEY" + "-" * 5
    end = "-" * 5 + "END PRIVATE KEY" + "-" * 5
    pem = f"{begin}\n{'X' * 40}\n{end}"
    out = redact_string(pem)
    assert "[PEM_PRIVATE_KEY_REDACTED]" in out


def test_jwt_redacted() -> None:
    """3-segment base64url JWT — built by concat to dodge scanners."""
    seg1 = "eyJ" + "A" * 20
    seg2 = "eyJ" + "B" * 30
    seg3 = "C" * 30 + "_" + "D" * 10
    jwt = f"{seg1}.{seg2}.{seg3}"
    out = redact_string(f"Authorization: Bearer {jwt}")
    assert "[JWT_REDACTED]" in out


# ── Regressions: non-secrets pass through ─────────────────────────


def test_normal_text_unchanged() -> None:
    assert redact_string("hello world") == "hello world"


def test_url_with_http_unchanged() -> None:
    """Plain URL must pass through (no false-positive matching)."""
    assert (
        redact_string("https://example.com/path?q=1")
        == "https://example.com/path?q=1"
    )


def test_short_strings_unchanged() -> None:
    """Short strings that LOOK like prefixes but lack the body length
    threshold don't get redacted (e.g. mention 'sk-' in docs)."""
    assert redact_string("the OpenAI key starts with sk-") == \
        "the OpenAI key starts with sk-"


# ── Recursive redact() over containers ─────────────────────────────


def test_redact_dict_recursive() -> None:
    inp = {
        "key": _mk("AIza", body_len=35),
        "nested": {
            "inner": _mk("sk-ant-", body_len=30),
        },
    }
    out = redact(inp)
    assert "AIza***" in out["key"]
    assert "sk-ant-***" in out["nested"]["inner"]


def test_redact_list_recursive() -> None:
    out = redact([
        _mk("sk-", body_len=30),
        ["nested", _mk("AIza", body_len=35)],
    ])
    assert "sk-***" in out[0]
    assert "AIza***" in out[1][1]
