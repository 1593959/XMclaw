"""Web UI scaffold guards (Epic #23 Phase 0).

These tests lock down the Phase-0 invariants so a sloppy edit doesn't
silently break the three-region shell or drop the vendored fallback
wiring described in ``docs/FRONTEND_DESIGN.md`` §11.3.1 (ADR-009).

Scope is deliberately narrow:

* Every file listed in the Phase-0 spec exists and is non-empty.
* ``index.html`` links the three base stylesheets + ``bootstrap.js``
  in the order the bootstrap relies on.
* ``bootstrap.js`` probes both CDN and vendor paths (so a rename on
  either side won't leak past review).
* Atom JS modules all import ``preact`` + ``htm`` from the
  ``window.__xmc`` handle exposed by ``bootstrap.js`` — this is our
  runtime contract.
* Static assets are reachable through the existing ``/ui`` mount
  (no new backend wiring needed for Phase 0).
* No JS / CSS source file exceeds the 500-line hard limit from
  FRONTEND_DESIGN.md §1.4.

Anything beyond this belongs to Phase 1 (WS wiring) or later.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from xmclaw.core.bus import InProcessEventBus
from xmclaw.daemon.app import create_app

STATIC_DIR = Path(__file__).resolve().parents[2] / "xmclaw" / "daemon" / "static"

REQUIRED_FILES = [
    "index.html",
    "bootstrap.js",
    "app.js",
    "router.js",
    "store.js",
    "AGENTS.md",
    "styles/tokens.css",
    "styles/reset.css",
    "styles/layout.css",
    "components/atoms/atoms.css",
    "components/atoms/button.js",
    "components/atoms/badge.js",
    # B-303: components/atoms/icon.js was deleted (zero call sites).
    # If a future page imports an Icon atom, restore the file + add
    # back to this list.
    "components/atoms/avatar.js",
    "components/atoms/spinner.js",
]

ATOM_MODULES = [
    "components/atoms/button.js",
    "components/atoms/badge.js",
    "components/atoms/avatar.js",
    "components/atoms/spinner.js",
]


# ── File presence ──────────────────────────────────────────────────────


@pytest.mark.parametrize("relpath", REQUIRED_FILES)
def test_required_file_present(relpath: str) -> None:
    path = STATIC_DIR / relpath
    assert path.is_file(), f"missing Phase-0 scaffold file: {relpath}"
    assert path.stat().st_size > 0, f"scaffold file is empty: {relpath}"


def test_vendor_dir_present_even_if_empty() -> None:
    # vendor/*.js is gitignored (populated by scripts/fetch_vendor.py),
    # but the directory itself must ship so the bootstrap fallback has a
    # resolvable path. We enforce this via a committed .gitkeep.
    gitkeep = STATIC_DIR / "vendor" / ".gitkeep"
    assert gitkeep.is_file(), "vendor/.gitkeep must ship so vendor/ exists on install"


# ── index.html structure ───────────────────────────────────────────────


def test_index_html_wires_styles_and_bootstrap() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    # Phase B replaced the legacy 3-region shell stylesheets with a
    # 1:1 port of Hermes's design system. The new ordering puts the
    # Hermes tokens first (so every later file can reference its
    # 3-layer palette / shadcn-compat vars), then the Hermes shell
    # styles. atoms.css remains last because it consumes the tokens.
    ordered_links = [
        "./styles/hermes-tokens.css",
        "./styles/reset.css",
        "./styles/hermes-backdrop.css",
        "./styles/hermes-shell.css",
        "./components/atoms/atoms.css",
    ]
    last_pos = -1
    for href in ordered_links:
        pos = html.find(href)
        assert pos > 0, f"index.html missing stylesheet link: {href}"
        assert pos > last_pos, (
            f"stylesheet {href} must appear after the previous one; tokens first rule"
        )
        last_pos = pos
    # Bootstrap script is loaded as a module.
    assert '<script type="module" src="./bootstrap.js"></script>' in html
    # Root mount + a11y noscript fallback both present.
    assert 'id="root"' in html
    assert "<noscript>" in html


# ── bootstrap.js dual-track contract ───────────────────────────────────


def test_bootstrap_probes_both_cdn_and_vendor() -> None:
    src = (STATIC_DIR / "bootstrap.js").read_text(encoding="utf-8")
    assert "https://esm.sh/preact@10" in src, "CDN preact URL must be pinned"
    assert "https://esm.sh/htm@3" in src, "CDN htm URL must be pinned"
    assert "./vendor/preact.min.js" in src, "vendor preact fallback must be present"
    assert "./vendor/htm.min.js" in src, "vendor htm fallback must be present"
    # Forced-local escape hatch (ADR-009).
    assert "xmc_assets_mode" in src
    # Source indicator recorded for the mini-devtools overlay.
    assert "xmc_bootstrap_source" in src


# ── atom modules consume Preact through window.__xmc ───────────────────


@pytest.mark.parametrize("relpath", ATOM_MODULES)
def test_atom_module_uses_window_xmc(relpath: str) -> None:
    src = (STATIC_DIR / relpath).read_text(encoding="utf-8")
    # Every atom must pull Preact + htm from the shared bootstrap handle —
    # never from an import or a global. This is what makes Phase 0's
    # dual-track loader work for every component.
    assert "window.__xmc.preact" in src, f"{relpath} must read preact from window.__xmc"
    assert "window.__xmc.htm.bind" in src, f"{relpath} must bind htm via window.__xmc"
    assert src.count("export function") >= 1, f"{relpath} must export a component"


def test_icon_atom_covers_every_sidebar_glyph() -> None:
    # Phase B moved the sidebar from app.js into the Hermes-port
    # AppShell organism (components/organisms/AppShell.js). Hermes's
    # sidebar uses an inline lucide-style ICONS map keyed by PascalCase
    # names (Terminal / MessageSquare / Sparkles / ...), so the legacy
    # icon-atom diff no longer applies. We instead lock down that the
    # NAV_ITEMS list references icons that are all defined in the
    # AppShell ICONS map (no missing-glyph regressions).
    import re

    shell_src = (
        STATIC_DIR / "components" / "organisms" / "AppShell.js"
    ).read_text(encoding="utf-8")

    # NAV_ITEMS:  icon: "Terminal"  →  must match a key in ICONS
    sidebar_icons: set[str] = set(
        re.findall(r"""icon:\s*["']([A-Za-z0-9_-]+)["']""", shell_src)
    )

    defined_icons: set[str] = set()
    in_map = False
    for line in shell_src.splitlines():
        stripped = line.strip()
        if stripped.startswith("const ICONS = {"):
            in_map = True
            continue
        if in_map and stripped.startswith("};"):
            break
        if not in_map:
            continue
        m = re.match(r"""([A-Za-z0-9_]+)\s*:\s*[\"']""", stripped)
        if m:
            defined_icons.add(m.group(1))

    assert sidebar_icons, "sidebar icon extraction regex matched nothing"
    missing = sidebar_icons - defined_icons
    assert not missing, f"AppShell ICONS map missing glyphs: {sorted(missing)}"


# ── file-size guard (FRONTEND_DESIGN.md §1.4) ──────────────────────────


# B-323: glob coverage was previously narrow — only ``*.js`` /
# ``components/atoms/*.js`` / ``styles/*.css`` were checked, so the
# 500-line cap silently DIDN'T apply to ``components/molecules/*.js``,
# ``components/organisms/*.js``, ``pages/*.js``, ``pages/_panels/*.js``,
# or ``lib/*.js``. Files in those directories had grown to 600-880 lines
# unchecked. The expanded patterns below close that hole for everyone
# except the explicit grandfather list (``KNOWN_OVERSIZED``) below.
SOURCE_GLOBS = [
    "*.js", "*.css", "*.html",
    "components/atoms/*.js", "components/atoms/*.css",
    "components/molecules/*.js",
    "components/organisms/*.js",
    "pages/*.js",
    "pages/_panels/*.js",
    "lib/*.js",
    "styles/*.css",
]
LINE_BUDGET = 500

# Files we know are over budget but haven't fully split yet — each
# entry MUST cite a follow-up plan. Empty dict = no exceptions, every
# file under SOURCE_GLOBS must clear the cap.
KNOWN_OVERSIZED: dict[str, str] = {
    # B-323 follow-up cleared the original entry
    # ``pages/_panels/memory_providers.js`` (was ~700 lines, now under
    # 500) by extracting its 5 sub-cards (indexer / dream / pinned /
    # picker / switcher) into sibling files.
    # B-395 (2026-05-20): memory_facts_v2_graph.js at ~659 lines.
    # The D3 force-directed graph + sigma.js dual-renderer setup is
    # inherently coupled; splitting would create 3 tightly-bound
    # partial files with no independent consumers. Defer split until
    # a second graph type forces a generic renderer abstraction.
    "pages/_panels/memory_facts_v2_graph.js": (
        "659 lines — D3+sigma dual renderer; split deferred until "
        "second graph type forces generic abstraction (B-395)"
    ),
    # B-395 (2026-05-20): app.js is the root shell orchestrator
    # (routing + layout + global state). Splitting would fracture
    # the single-source-of-truth for mount/unmount lifecycle.
    "app.js": (
        "577 lines — root shell orchestrator; split deferred until "
        "layout engine is extracted (B-395)"
    ),
    # B-395 (2026-05-20): Composer.js handles input mode switching
    # (text / voice / image / file) + send orchestration + paste
    # handling. Each mode's UI is already a separate atom; the
    # orchestration glue is what remains.
    "components/molecules/Composer.js": (
        "540 lines — input-mode orchestration glue; atoms already "
        "split, glue extraction needs design pass (B-395)"
    ),
    # B-395 (2026-05-20): chat_reducer.js is a state-machine reducer
    # with 12 action types. Splitting by action type would create
    # 12 single-function files with heavy shared-type imports.
    "lib/chat_reducer.js": (
        "580 lines — 12-action reducer state machine; split by "
        "action type creates import-churn without clarity win (B-395)"
    ),
    # B-395 (2026-05-20): chat.css is a theme file with extensive
    # variable declarations + component overrides. Purely declarative;
    # split by component would require CSS cascade reordering.
    "styles/chat.css": (
        "634 lines — theme variable + override declarations; "
        "declarative CSS split risks cascade reorder bugs (B-395)"
    ),
    # Phase F (2026-05-22): LanguageSwitcher grew from a 7-line stub to
    # a functional dropdown (~80 lines). Splitting it out creates a
    # 90-line molecule with no other consumers. Defer until a second
    # sidebar footer widget justifies a shared dropdown primitive.
    "components/organisms/AppShellParts.js": (
        "~502 lines — LanguageSwitcher dropdown added; split deferred "
        "until second footer widget justifies shared primitive (Phase F)"
    ),
}


def test_no_file_exceeds_line_budget() -> None:
    offenders: list[tuple[str, int]] = []
    for pattern in SOURCE_GLOBS:
        for path in STATIC_DIR.glob(pattern):
            if not path.is_file():
                continue
            rel = str(path.relative_to(STATIC_DIR)).replace("\\", "/")
            if rel in KNOWN_OVERSIZED:
                continue
            count = sum(1 for _ in path.open("r", encoding="utf-8"))
            if count > LINE_BUDGET:
                offenders.append((rel, count))
    assert not offenders, (
        f"Files exceeding {LINE_BUDGET}-line budget: {offenders}. "
        "Split into molecules / organisms per FRONTEND_DESIGN.md §1.4. "
        "If the file genuinely cannot be split right now, add it to "
        "KNOWN_OVERSIZED with a one-line follow-up plan — but expect "
        "review pushback if the list grows."
    )


def test_known_oversized_files_actually_exist() -> None:
    """A KNOWN_OVERSIZED entry that no longer points to a real file
    is dead weight — clean it out instead of accumulating cruft."""
    for rel in KNOWN_OVERSIZED:
        path = STATIC_DIR / rel
        assert path.is_file(), (
            f"KNOWN_OVERSIZED references {rel!r} but it no longer "
            "exists — drop the entry from the dict."
        )


def test_known_oversized_files_actually_oversized() -> None:
    """Conversely, if a file in KNOWN_OVERSIZED has been split down
    below the budget, it should be removed from the list — keeping
    a stale entry is technical debt that hides regressions."""
    for rel in KNOWN_OVERSIZED:
        path = STATIC_DIR / rel
        if not path.is_file():
            continue  # caught by the previous test
        count = sum(1 for _ in path.open("r", encoding="utf-8"))
        assert count > LINE_BUDGET, (
            f"{rel!r} is now {count} lines (≤ {LINE_BUDGET}) — drop "
            "it from KNOWN_OVERSIZED so future regressions actually "
            "trip the budget check."
        )


# ── HTTP reachability via the existing /ui mount ───────────────────────


@pytest.fixture
def http_client() -> TestClient:
    bus = InProcessEventBus()
    return TestClient(create_app(bus=bus))


@pytest.mark.parametrize(
    "url, needle",
    [
        ("/ui/index.html", "XMclaw"),
        ("/ui/bootstrap.js", "xmc_bootstrap_source"),
        # Phase B moved sidebar items into the AppShell organism.
        ("/ui/app.js", "HermesAppShell"),
        ("/ui/components/organisms/AppShell.js", "NAV_ITEMS"),
        ("/ui/router.js", "installRouter"),
        ("/ui/store.js", "createStore"),
        ("/ui/styles/tokens.css", "--xmc-accent"),
        ("/ui/components/atoms/button.js", "export function Button"),
    ],
)
def test_ui_assets_served_under_ui_mount(
    http_client: TestClient, url: str, needle: str
) -> None:
    resp = http_client.get(url)
    assert resp.status_code == 200, f"{url} should be 200; got {resp.status_code}"
    assert needle in resp.text, f"{url} body missing expected marker {needle!r}"


# ── B-344 (audit pass-2 follow-up): import-path resolution ────────
#
# Static-file paths in ESM imports are resolved relative to the
# importing module. A panel file at ``pages/_panels/foo.js`` doing
# ``import x from "../lib/api.js"`` resolves to
# ``/ui/pages/lib/api.js`` — which doesn't exist; the daemon's SPA
# fallback returns the index HTML and the browser rejects the
# import as MIME-mismatched (``application/javascript`` expected,
# ``text/html`` received). The whole import graph collapses, and
# the user gets a blank black page with a console full of
# "Failed to fetch dynamically imported module" errors.
#
# This test scans every JS file in static/ for relative-path
# imports and asserts the target file exists on disk. Catches the
# kind of bug the line-budget split introduced (B-323 panel
# extractions where ``../lib/...`` was wrong from one-level-deeper
# ``_panels/``). Doesn't try to type-check exports — just resolve.

import re  # noqa: E402

_IMPORT_RE = re.compile(
    r'(?:^|[\s;,(])import\s+(?:[^"\']*from\s+)?["\']([^"\']+)["\']'
    r'|(?:^|[\s;,])from\s+["\']([^"\']+)["\']',
    re.MULTILINE,
)


def _resolve_import(source_file: Path, spec: str) -> Path | None:
    """Resolve a JS import spec ``./foo.js`` / ``../bar.js`` /
    ``./qux/index.js`` against ``source_file``'s directory. Returns
    ``None`` for non-relative specs (CDN URLs, bare module names —
    those have their own resolution rules)."""
    if not spec.startswith(("./", "../")):
        return None
    # Strip any cache-busting query the runtime appends.
    spec = spec.split("?", 1)[0]
    return (source_file.parent / spec).resolve()


def _all_js_files() -> list[Path]:
    return sorted(STATIC_DIR.rglob("*.js"))


def test_b344_every_js_file_parses_with_node_check() -> None:
    """Every JS file under ``static/`` must parse as a valid ES module
    via ``node --check``. The B-344 regression that triggered this
    test was a B-323 monolith split that lost a function header from
    ``memory_providers.js`` — leaving ``useState`` /
    ``useEffect`` / ``return`` calls at module top-level. JavaScript
    rejected the file with ``Uncaught SyntaxError: Illegal return
    statement`` only when the browser tried to parse it; the build
    pipeline (line-budget + import-path tests) didn't catch it
    because they don't invoke a parser.

    Skips when ``node`` isn't on PATH so contributors without
    Node.js can still run the suite — the actual frontend doesn't
    require Node either (no build step). CI must keep Node
    installed for this test to fire.
    """
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH — skipping JS parse check")

    failures: list[tuple[str, str]] = []
    for js in _all_js_files():
        rel = str(js.relative_to(STATIC_DIR))
        try:
            cp = subprocess.run(
                [node, "--check", str(js)],
                capture_output=True, text=True, timeout=20,
            )
        except subprocess.TimeoutExpired:
            failures.append((rel, "node --check timed out"))
            continue
        if cp.returncode != 0:
            # node --check writes errors to stderr; truncate so the
            # assertion message stays readable.
            err = (cp.stderr or cp.stdout).strip().splitlines()
            failures.append((rel, "\n    ".join(err[:6])))
    assert not failures, (
        "JS files failed node --check (browser will reject these as "
        "SyntaxError, blanking the UI):\n  "
        + "\n  ".join(f"{rel}\n    {msg}" for rel, msg in failures)
    )


def test_b345_chat_reducer_actually_creates_card_via_node() -> None:
    """B-345 stronger sibling — actually load and run chat_reducer.js
    in Node and assert the reducer SHAPE: feeding an
    ``agent_asked_question`` event must yield a ``kind="question"``
    message; the follow-up ``user_answered_question`` event must
    flip its ``status`` to ``"complete"`` and stash the answer.

    Skips when ``node`` isn't on PATH. This catches what the
    regex-only sibling can't: a typo in the payload-key plumbing,
    a wrong message-id scheme, missing import-graph deps, or a
    malformed return value.
    """
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH")

    static = STATIC_DIR.resolve().as_posix()

    # Inline a Node ESM driver that:
    #   1. polyfills the ``window.__xmc`` handle the reducer reads
    #      at module-load time (preact + hooks + htm bind),
    #   2. dynamic-imports chat_reducer.js,
    #   3. runs two events through ``applyEvent``,
    #   4. prints the resulting state as JSON for the test to assert on.
    driver = f"""
    const url = "file:///{static}/lib/chat_reducer.js";
    // Minimum stubs the reducer module reads at top-level. The
    // streaming + secondary sub-reducers also import preact_hooks /
    // htm; stub them to no-op return values so module resolution
    // succeeds without bringing in the whole ecosystem.
    globalThis.window = globalThis.window || {{}};
    globalThis.window.__xmc = {{
      preact: {{ h: () => null }},
      preact_hooks: {{
        useState: (init) => [init, () => {{}}],
        useEffect: () => {{}},
        useMemo: (fn) => fn(),
        useCallback: (fn) => fn,
      }},
      htm: {{ bind: () => () => null }},
    }};
    const mod = await import(url);
    let state = {{ messages: [] }};
    state = mod.applyEvent(state, {{
      type: "agent_asked_question",
      payload: {{
        question_id: "Q1",
        question: "delete N skills?",
        options: [
          {{ value: "yes", label: "yes" }},
          {{ value: "no", label: "no" }},
        ],
        multi_select: false,
        allow_other: true,
        tool_call_id: "tc-1",
      }},
      ts: 1000,
    }});
    state = mod.applyEvent(state, {{
      type: "user_answered_question",
      payload: {{ question_id: "Q1", value: "yes" }},
      ts: 1001,
    }});
    process.stdout.write(JSON.stringify(state));
    """
    cp = subprocess.run(
        [node, "--input-type=module", "-e", driver],
        capture_output=True, text=True, timeout=30,
    )
    assert cp.returncode == 0, (
        f"node driver failed: stdout={cp.stdout!r} stderr={cp.stderr!r}"
    )
    state = json.loads(cp.stdout)
    msgs = state.get("messages", [])
    assert len(msgs) == 1, f"expected one card, got {msgs!r}"
    card = msgs[0]
    assert card.get("kind") == "question"
    assert card.get("status") == "complete"
    assert card.get("answer") == "yes"
    q = card.get("question") or {}
    assert q.get("id") == "Q1"
    assert q.get("question") == "delete N skills?"
    assert q.get("multi_select") is False
    assert q.get("tool_call_id") == "tc-1"
    assert isinstance(q.get("options"), list) and len(q["options"]) == 2


def test_b345_chat_reducer_handles_agent_asked_question() -> None:
    """B-345: ``chat_reducer.js`` must have an explicit ``case
    "agent_asked_question":`` branch that builds a question card.

    Pre-B-345 the event was in ``PHASE_1_EVENT_TYPES`` (so the WS
    layer recognised it) but the reducer's switch had no case — the
    event silently fell through to ``default`` → no card appeared.
    The QuestionCard ONLY ever rendered via the recovery path
    (``rehydratePendingQuestions`` GET against
    ``/api/v2/pending_questions``), which fires on WS connect — so
    a question issued mid-session was invisible until the user
    refreshed the tab.

    Both branches are pinned: ``agent_asked_question`` (live arrival
    creates the card) and ``user_answered_question`` (post-answer
    flips the card to read-only). A future refactor that drops
    either case will fail this test.
    """
    src = (STATIC_DIR / "lib" / "chat_reducer.js").read_text(encoding="utf-8")
    assert 'case "agent_asked_question"' in src, (
        'chat_reducer.js missing ``case "agent_asked_question"`` — '
        'live AGENT_ASKED_QUESTION events will silently no-op and '
        'the QuestionCard will only show after a tab refresh.'
    )
    assert 'case "user_answered_question"' in src, (
        'chat_reducer.js missing ``case "user_answered_question"`` — '
        'answered cards will not flip to read-only on echo, leaving '
        'a stale active card the user can submit twice.'
    )
    # The agent_asked_question branch must build a ``kind: "question"``
    # message so MessageBubble routes it to QuestionCard. Find the
    # branch body by locating the next ``case "...":`` or ``default:``
    # — must use a quoted-case marker so a literal "case" inside a
    # comment ("had no case — so …") doesn't mis-truncate the body.
    aaq_idx = src.index('case "agent_asked_question"')
    import re as _re
    next_marker = _re.search(r'(case "|default:)', src[aaq_idx + 1:])
    next_case = (aaq_idx + 1 + next_marker.start()) if next_marker else len(src)
    branch_body = src[aaq_idx:next_case]
    assert 'kind: "question"' in branch_body, (
        '``agent_asked_question`` branch must produce a message with '
        '``kind: "question"`` — otherwise MessageBubble won\'t render '
        'the QuestionCard component.'
    )


def test_b344_full_module_graph_resolves_via_test_client(
    http_client: TestClient,
) -> None:
    """Browser-equivalent module-graph walk via TestClient. Seeds at
    ``/ui/bootstrap.js`` + ``/ui/app.js`` (the two real entry points),
    parses every ``import`` / ``from`` spec, fetches each, and asserts
    every JS module is served with a JS Content-Type — never the SPA
    fallback HTML that B-344's underlying bug triggered. Catches:

      * ``../foo`` paths that drop one level too few (B-344 panel
        files: ``../lib`` from ``_panels/`` → ``/ui/pages/lib`` → 404
        → SPA fallback HTML → MIME mismatch → blank UI).
      * Files referenced but absent on disk.
      * MIME-type misconfiguration in the StaticFiles mount.

    The matching ``test_b344_every_relative_import_resolves_to_a_real_file``
    above does the same check against disk; this one verifies the
    SERVING layer too — they catch overlapping but different bug
    classes."""
    import re

    base = "/ui"
    seeds = [base + "/bootstrap.js", base + "/app.js"]
    visited: set[str] = set()
    failures: list[tuple[str, str]] = []

    import_re = re.compile(
        r'''import\s*(?:[^"';]*?from\s*)?["']([^"']+)["']'''
        r'''|import\s*\(\s*["']([^"']+)["']'''
        r'''|from\s*["']([^"']+)["']''',
        re.MULTILINE,
    )

    def _resolve(parent: str, spec: str) -> str | None:
        if spec.startswith(("http://", "https://", "//")):
            return None  # CDN — not our problem
        if not spec.startswith(("./", "../", "/")):
            return None  # bare module
        spec = spec.split("?", 1)[0]
        if spec.startswith("/"):
            return spec
        # parent is "/ui/.../foo.js"; resolve relative
        from posixpath import normpath
        parent_dir = parent.rsplit("/", 1)[0]
        return normpath(parent_dir + "/" + spec)

    def walk(url: str, parent: str = "(seed)") -> None:
        if url in visited:
            return
        visited.add(url)
        resp = http_client.get(url)
        if resp.status_code != 200:
            failures.append(
                (url, f"HTTP {resp.status_code} (parent={parent})")
            )
            return
        ct = resp.headers.get("content-type", "")
        if "text/html" in ct.lower():
            failures.append(
                (url, f"served as HTML ({ct}) — SPA fallback "
                      f"(parent={parent})")
            )
            return
        if "javascript" not in ct.lower():
            return  # CSS / JSON / etc — fine, just don't recurse
        body = resp.text
        for m in import_re.finditer(body):
            spec = next((g for g in m.groups() if g), None)
            if not spec:
                continue
            target = _resolve(url, spec)
            if target is None:
                continue
            walk(target, parent=url)

    for seed in seeds:
        walk(seed)

    assert not failures, (
        f"Module graph has {len(failures)} failure(s) — browser will "
        f"reject these with MIME/404 errors → blank UI:\n  "
        + "\n  ".join(f"{url}: {msg}" for url, msg in failures)
    )
    # Sanity floor: a real frontend has dozens of modules.
    assert len(visited) >= 30, (
        f"Walked only {len(visited)} modules — graph likely truncated "
        f"by an early failure that wasn't reported (visited={visited})"
    )


def test_b344_every_relative_import_resolves_to_a_real_file() -> None:
    """No JS file in the UI may import a relative path that doesn't
    exist on disk. Pre-B-344 three panel files (memory_identity.js,
    memory_notes_journal.js, settings_audio.js) shipped with
    one-level-too-shallow paths from the B-323 monolith split —
    ``../lib/api.js`` from ``_panels/`` resolves to a non-existent
    ``/ui/pages/lib/api.js``. The pages just hadn't been opened
    until B-341/B-342 and the user got a blank page."""
    static_root = STATIC_DIR.resolve()
    bad: list[tuple[str, str, Path]] = []
    for js in _all_js_files():
        try:
            text = js.read_text(encoding="utf-8")
        except OSError:
            continue
        for m in _IMPORT_RE.finditer(text):
            spec = m.group(1) or m.group(2)
            if not spec:
                continue
            target = _resolve_import(js, spec)
            if target is None:
                continue  # bare or absolute — skip
            # Defend against escaping the static root via excessive ``..``.
            try:
                target.relative_to(static_root)
            except ValueError:
                bad.append((str(js.relative_to(STATIC_DIR)), spec, target))
                continue
            if not target.is_file():
                bad.append((str(js.relative_to(STATIC_DIR)), spec, target))
    assert not bad, (
        "Relative imports point at non-existent files (browser will "
        "reject as MIME-mismatched HTML when the SPA fallback fires):\n  "
        + "\n  ".join(
            f"{src}: import {spec!r} → {target}"
            for src, spec, target in bad
        )
    )


# ── B-394: no double-backticks inside html`...` tagged templates ──
#
# htm uses backticks as the JS template-literal delimiter. A single
# bare backtick inside the body closes the template early; a double
# backtick (e.g. markdown-style ``code`` formatting in a comment)
# closes-then-reopens, corrupting the rest of the parse and producing
# a runtime ``html(...) is not a function`` crash from inside the
# component's render. The B-368 commit shipped exactly that bug —
# this guard prevents regressions across every UI source file.


def test_b394_no_double_backticks_inside_html_template_bodies() -> None:
    """``\\`\\``` (two literal backticks) inside an ``html\\``...\\``` body
    closes the JS template literal early. UI crashed with
    ``html(...) is not a function`` because of exactly this pattern
    in a B-368 explanatory HTML comment. Scan every .js source under
    static/ and refuse to ship if any line inside an html-template
    body contains ``\\`\\```.
    """
    offenders: list[tuple[str, int, str]] = []
    for js in STATIC_DIR.rglob("*.js"):
        if "/vendor/" in str(js).replace("\\", "/"):
            continue  # third-party, not ours
        src = js.read_text(encoding="utf-8")
        # Linear scan: track when we're inside an html`...` body.
        # Naive (doesn't handle nested templates perfectly) but
        # catches the actual class of bug — markdown ``code`` inside
        # a JS template literal.
        in_template = False
        for idx, line in enumerate(src.splitlines(), start=1):
            # Open: any line containing ``html`` opens (will also see
            # closes on same line via the count check below).
            opens = "html`" in line
            if opens and not in_template:
                in_template = True
            if in_template and "``" in line:
                offenders.append(
                    (str(js.relative_to(STATIC_DIR)), idx, line.strip()[:140]),
                )
            # Close heuristic: any line with an odd number of `\`` likely
            # closes the template. This is approximate but good enough
            # for a regression guard — false positives just mean we
            # stop scanning early on a file, missing later offenses,
            # but the test catches the FIRST offense which is what
            # matters for crash prevention.
            if in_template and (line.count("`") % 2 == 1) and not opens:
                in_template = False
    assert not offenders, (
        "Found ``...`` (double-backtick) inside an html`...` tagged "
        "template body. JS parses these as 'close-template + reopen "
        "template', producing 'html(...) is not a function' at "
        "render time. Use plain quotes or move the comment outside "
        "the template:\n  "
        + "\n  ".join(f"{f}:{ln}: {body}" for f, ln, body in offenders)
    )
