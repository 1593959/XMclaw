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
    "components/atoms/icon.js",
    "components/atoms/avatar.js",
    "components/atoms/spinner.js",
]

ATOM_MODULES = [
    "components/atoms/button.js",
    "components/atoms/badge.js",
    "components/atoms/icon.js",
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


SOURCE_GLOBS = ["*.js", "*.css", "*.html", "components/atoms/*.js", "components/atoms/*.css", "styles/*.css"]
LINE_BUDGET = 500


def test_no_file_exceeds_line_budget() -> None:
    offenders: list[tuple[str, int]] = []
    for pattern in SOURCE_GLOBS:
        for path in STATIC_DIR.glob(pattern):
            if not path.is_file():
                continue
            count = sum(1 for _ in path.open("r", encoding="utf-8"))
            if count > LINE_BUDGET:
                offenders.append((str(path.relative_to(STATIC_DIR)), count))
    assert not offenders, (
        f"Files exceeding {LINE_BUDGET}-line budget: {offenders}. "
        "Split into molecules / organisms per FRONTEND_DESIGN.md §1.4."
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
