"""B-355 (Sprint 1): Origin header validation for WS + HTTP.

Defense against ClawJacked-style attacks (a malicious page in the
user's browser making cross-origin requests to the loopback daemon).
Browsers send ``Origin`` on cross-origin requests; non-browser
clients (curl / SDKs / CLI) typically don't send it. The daemon
allows:
  * No Origin (CLI / SDK)
  * ``null`` (file://, native shells)
  * Loopback (127.0.0.1 / localhost / ::1) on any port
  * Explicitly opted-in origins via ``gateway.allowed_origins``
And rejects everything else.
"""
from __future__ import annotations

from xmclaw.daemon.app import _origin_allowed


# ── Default-allow cases (browser CLI legitimate) ─────────────────


def test_b355_no_origin_allowed() -> None:
    """Curl / Python SDK / CLI clients don't send Origin. Must work."""
    assert _origin_allowed(None, {})


def test_b355_null_origin_allowed() -> None:
    """file:// and sandboxed iframes send Origin: null. Must work
    so PWA / desktop tray / Tauri apps still connect."""
    assert _origin_allowed("null", {})


def test_b355_loopback_127_allowed() -> None:
    """The Web UI itself sends ``http://127.0.0.1:8765``. Must allow."""
    assert _origin_allowed("http://127.0.0.1:8765", {})


def test_b355_loopback_localhost_allowed() -> None:
    """Browsers may resolve via ``localhost`` instead of 127.0.0.1.
    Must allow regardless of port."""
    assert _origin_allowed("http://localhost:3000", {})


def test_b355_loopback_ipv6_allowed() -> None:
    """IPv6 loopback ``[::1]``. Must allow."""
    assert _origin_allowed("http://[::1]:8765", {})


def test_b355_loopback_https_allowed() -> None:
    """TLS loopback (rare but legitimate). Must allow."""
    assert _origin_allowed("https://127.0.0.1", {})


# ── Default-reject cases (CSRF attack vectors) ────────────────────


def test_b355_external_origin_rejected() -> None:
    """The ClawJacked attack vector: malicious page in user's
    browser. Must reject."""
    assert not _origin_allowed("http://evil.com", {})


def test_b355_lan_origin_rejected_by_default() -> None:
    """LAN origins are rejected unless explicitly opted in. A user
    on 192.168.x.x exposing the daemon non-loopback should know
    they need the allowlist."""
    assert not _origin_allowed("http://192.168.1.10:8765", {})


def test_b355_https_external_rejected() -> None:
    assert not _origin_allowed("https://evil.com", {})


# ── Config opt-in ────────────────────────────────────────────────


def test_b355_explicit_lan_origin_allowed_via_config() -> None:
    """Operator with intentional LAN exposure can allowlist via
    ``gateway.allowed_origins``."""
    cfg = {"gateway": {"allowed_origins": ["http://192.168.1.10:8765"]}}
    assert _origin_allowed("http://192.168.1.10:8765", cfg)


def test_b355_allowlist_does_not_match_partial() -> None:
    """Allowlist matches must be exact origin. ``http://evil.com``
    must not be allowed because ``http://evil.com:1234`` is
    listed."""
    cfg = {"gateway": {"allowed_origins": ["http://evil.com:1234"]}}
    assert not _origin_allowed("http://evil.com", cfg)


def test_b355_malformed_origin_rejected() -> None:
    """Garbage origin (e.g. injection attempt) → reject."""
    # Not a valid URL — urlparse may not raise but hostname is empty.
    assert not _origin_allowed("not-a-url", {})


def test_b355_empty_string_origin_rejected() -> None:
    """Empty string is the explicit "no origin" sentinel some
    browsers / proxies send. Treat as None (allowed) — non-browser
    client."""
    # Empty string is falsy → first branch returns True.
    assert _origin_allowed("", {})


def test_b355_origin_case_insensitive_host() -> None:
    """Hostname comparison must be case-insensitive (RFC 3986
    says hostnames are case-insensitive)."""
    assert _origin_allowed("http://LOCALHOST:3000", {})
    assert _origin_allowed("http://127.0.0.1:8765", {})  # already lowercase
