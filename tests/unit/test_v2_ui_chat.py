"""Phase 1 Chat workspace — static-scan contract tests.

We have no Node.js / no jsdom — but the wiring between the JS modules is
load-bearing enough that we want regression coverage. These tests scan
the source files for the specific tokens that mark a working contract:

  * ``app.js`` imports the WS client + chat reducer + ChatPage and wires
    them to ``store.setState``.
  * ``lib/ws.js`` builds the WS URL with the correct query-param shape and
    handles reconnect / auth_failed (codes 4401 / 4403).
  * ``lib/auth.js`` calls ``/api/v2/pair`` and only that.
  * ``lib/chat_reducer.js`` exports ``applyEvent``, ``applySessionLifecycle``
    and ``appendOptimisticUser``, and handles the Phase-1 essential
    EventType set.
  * ``lib/markdown.js`` escapes HTML before splicing fenced-code blocks
    back in (XSS safety) and renders fences + inline code.
  * ``components/molecules/*.js`` import Preact via ``window.__xmc`` like
    the atoms do.

A future move from static scan to a real e2e Playwright pass would relax
these — until then they're our only fence against silent module-rename
breakage.
"""
from __future__ import annotations

from pathlib import Path

import pytest

STATIC_DIR = Path(__file__).resolve().parents[2] / "xmclaw" / "daemon" / "static"


def read(rel: str) -> str:
    return (STATIC_DIR / rel).read_text(encoding="utf-8")


# ── app.js wires the Phase-1 surface ───────────────────────────────────


def test_app_js_imports_phase1_modules() -> None:
    src = read("app.js")
    assert "from \"./lib/auth.js\"" in src or "from './lib/auth.js'" in src
    assert "from \"./lib/ws.js\"" in src or "from './lib/ws.js'" in src
    assert "from \"./lib/chat_reducer.js\"" in src or "from './lib/chat_reducer.js'" in src
    assert "from \"./pages/Chat.js\"" in src or "from './pages/Chat.js'" in src


def test_app_js_chat_route_uses_chat_page() -> None:
    src = read("app.js")
    # /chat route must render the real ChatPage, not a Placeholder.
    assert "\"/chat\"" in src
    assert "ChatPage" in src, "app.js must use ChatPage for /chat"
    # Make sure the placeholder line for /chat is gone.
    assert (
        '"/chat":' in src
        and "title=\"Chat\" subtitle=\"会话主面板" not in src
    ), "Phase 0 placeholder for /chat must be replaced"


def test_app_js_dispatches_events_into_chat_reducer() -> None:
    src = read("app.js")
    # We expect setState wiring that calls applyEvent / applySessionLifecycle
    # in the WS onEvent callback. The exact whitespace can vary, just look
    # for the function names being applied.
    assert "applyEvent" in src
    assert "applySessionLifecycle" in src
    assert "appendOptimisticUser" in src


# ── lib/ws.js: URL shape, reconnect, auth_failed codes ─────────────────


def test_ws_module_builds_correct_url_pattern() -> None:
    src = read("lib/ws.js")
    assert "/agent/v2/" in src, "WS endpoint must be /agent/v2/{sid}"
    # Token query-param contract — the daemon's pairing.py reads ?token=...
    assert "searchParams.set(\"token\"" in src
    # ws / wss switch based on page protocol.
    assert "wss:" in src and "ws:" in src
    assert "https:" in src


def test_ws_module_handles_auth_failed_codes() -> None:
    src = read("lib/ws.js")
    # 4401 = pairing token rejected (xmclaw/daemon/pairing.py contract).
    # 4403 = pairing disabled / token-not-permitted variant.
    assert "4401" in src, "WS must surface 4401 as auth_failed"
    assert "4403" in src
    assert "auth_failed" in src


def test_ws_module_has_exponential_backoff_with_cap() -> None:
    src = read("lib/ws.js")
    # Simple sanity: there's a reconnect with both a base and a max ms.
    assert "RECONNECT_BASE_MS" in src
    assert "RECONNECT_MAX_MS" in src
    assert "Math.pow(2," in src, "must use exponential backoff"


def test_ws_module_exposes_test_seams() -> None:
    """The static scaffold tests inject a fake socket factory so the test
    suite never hits a real network. ws.js must accept ``socketFactory``
    and ``maxReconnects`` parameters."""
    src = read("lib/ws.js")
    assert "socketFactory" in src
    assert "maxReconnects" in src


# ── lib/auth.js: same-origin /api/v2/pair fetch ────────────────────────


def test_auth_module_targets_pair_endpoint_only() -> None:
    src = read("lib/auth.js")
    assert "/api/v2/pair" in src
    assert "credentials: \"same-origin\"" in src
    # No third-party endpoint — nobody should be sneaking in a leak path.
    assert "http://" not in src
    assert "https://" not in src


# ── lib/chat_reducer.js: pure mapping from events to chat slice ───────


def test_chat_reducer_handles_phase1_event_set() -> None:
    src = read("lib/chat_reducer.js")
    for et in [
        "user_message",
        "llm_chunk",
        "llm_response",
        "tool_call_emitted",
        "tool_invocation_finished",
        "anti_req_violation",
        "session_lifecycle",
    ]:
        assert f'"{et}"' in src, f"chat_reducer must handle {et}"


def test_chat_reducer_exports_three_helpers() -> None:
    src = read("lib/chat_reducer.js")
    assert "export function applyEvent" in src
    assert "export function applySessionLifecycle" in src
    assert "export function appendOptimisticUser" in src


def test_chat_reducer_exports_phase1_event_list() -> None:
    """Code that iterates the supported event set should import this list
    rather than hard-coding strings."""
    src = read("lib/chat_reducer.js")
    assert "PHASE_1_EVENT_TYPES" in src


# ── lib/markdown.js: XSS safety + fence support ────────────────────────


def test_markdown_renderer_escapes_html_first() -> None:
    src = read("lib/markdown.js")
    # The HTML escape map and its consumer must be present BEFORE the
    # inline / fence transformation routines run.
    assert "escapeHtml" in src
    # All five HTML special chars covered.
    assert '"&"' in src
    assert '"<"' in src
    assert '">"' in src
    assert "'\"'" in src or '"\\""' in src
    # Fenced block extraction prefix.
    assert "extractCodeBlocks" in src
    assert "```" in src


def test_markdown_renderer_supports_inline_and_fences() -> None:
    src = read("lib/markdown.js")
    # Inline code: backtick-delimited.
    assert "xmc-md__code" in src
    # Fenced block class name (used by chat.css).
    assert "xmc-md__pre" in src
    # Bold / italic / autolink.
    assert "<strong>" in src
    assert "<em>" in src
    assert "rel=\"noopener noreferrer\"" in src


# ── molecules import via window.__xmc handle (same as atoms) ───────────


@pytest.mark.parametrize(
    "rel",
    [
        "components/molecules/MessageBubble.js",
        "components/molecules/MessageList.js",
        "components/molecules/Composer.js",
        "pages/Chat.js",
    ],
)
def test_molecule_uses_window_xmc(rel: str) -> None:
    src = read(rel)
    assert "window.__xmc.preact" in src, f"{rel} must read preact from window.__xmc"
    assert "window.__xmc.htm.bind" in src, f"{rel} must bind htm via window.__xmc"


def test_message_bubble_renders_via_dangerouslySetInnerHTML() -> None:
    """The reducer hands the escape-then-format pipeline a sanitized HTML
    string; the bubble must inject it via dangerouslySetInnerHTML, not as
    a child node (which would render the raw HTML as text)."""
    src = read("components/molecules/MessageBubble.js")
    assert "dangerouslySetInnerHTML" in src
    assert "renderMarkdown" in src


def test_composer_send_keybindings_present() -> None:
    """Enter sends, Shift+Enter newline, Esc blurs — locking these so a
    refactor doesn't accidentally drop the IME-safe `isComposing` guard."""
    src = read("components/molecules/Composer.js")
    assert "evt.key === \"Enter\"" in src
    assert "evt.shiftKey" in src
    assert "evt.isComposing" in src
    assert "Escape" in src


# ── store slices include the chat workspace ────────────────────────────


def test_store_includes_phase1_slices() -> None:
    src = read("store.js")
    for needle in [
        "auth: { token:",
        "chat:",
        "messages:",
        "pendingAssistantId",
        "composerDraft",
        "planMode",
        "ultrathink",
        "newSid",
        "persistActiveSid",
        "persistSidList",
    ]:
        assert needle in src, f"store.js missing Phase-1 surface: {needle!r}"
