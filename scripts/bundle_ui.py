"""XMclaw UI bundler — merge 115+ JS/CSS files into single bundles.

Usage:
    python scripts/bundle_ui.py              # build bundles
    python scripts/bundle_ui.py --watch      # watch mode for dev
    python scripts/bundle_ui.py --minify     # production minified build

Output:
    xmclaw/daemon/static/dist/bundle.js     — all JS (Preact + htm + app)
    xmclaw/daemon/static/dist/bundle.css    — all CSS

The daemon's StaticFiles mount should check dist/ first, fall back to
the individual files if the bundle doesn't exist. This keeps the
no-build dev workflow intact — the bundler is an optimisation, not
a requirement.

Dependency: ``pip install esbuild`` (pip-installable Python wrapper,
~5 MB, no Node.js required — wraps the platform-native esbuild binary).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "xmclaw" / "daemon" / "static"
DIST_DIR = STATIC_DIR / "dist"
APP_ENTRY = STATIC_DIR / "app.js"
CSS_GLOB = STATIC_DIR / "styles" / "*.css"
COMPONENT_CSS_GLOB = STATIC_DIR / "components" / "**" / "*.css"


def bundle_js(minify: bool = False, watch: bool = False) -> None:
    """Bundle all JS via esbuild."""
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    outfile = DIST_DIR / "bundle.js"

    cmd = [
        sys.executable, "-m", "esbuild",
        str(APP_ENTRY),
        "--bundle",
        f"--outfile={outfile}",
        "--format=esm",
        "--platform=browser",
        "--target=es2020",
        # Let Preact+htm resolve from the global __xmc namespace
        # (window.__xmc.preact / window.__xmc.htm) rather than
        # bundling them inline. This keeps the CDN-first approach
        # intact — esbuild only bundles our own code, not the
        # framework.
        "--external:preact",
        "--external:htm",
        "--external:preact/hooks",
    ]
    if minify:
        cmd.append("--minify")
    if watch:
        cmd.append("--watch")

    print(f"[bundle] JS → {outfile}")
    subprocess.run(cmd, check=True, cwd=str(STATIC_DIR))


def bundle_css(minify: bool = False) -> None:
    """Concatenate all CSS files into one bundle.

    esbuild supports CSS bundling natively, but our CSS files are
    plain imports (no @import chains). We concatenate them in dependency
    order so the cascade is preserved.
    """
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    outfile = DIST_DIR / "bundle.css"

    # Order matters: design tokens first, then components, then pages,
    # then overrides last (mobile + unify-fx).
    css_order: list[Path] = []
    styles_dir = STATIC_DIR / "styles"
    for name in [
        "reset.css",
        "theme-claw.css",
        "theme-nebula.css",
        "design-system.css",
        "tokens.css",
        "global-v2.css",
        "chat.css",
        "chat-v2.css",
        "chat-markdown.css",
        "chart-phase.css",
        "layout.css",
        "toast.css",
        "workspace.css",
        "workspace-panel.css",
        "agent-ui.css",
    ]:
        fp = styles_dir / name
        if fp.exists():
            css_order.append(fp)
    # Append any remaining stylesheets not in the explicit order.
    for fp in sorted(styles_dir.glob("*.css")):
        if fp not in css_order:
            css_order.append(fp)
    # Append component CSS files.
    for fp in sorted(STATIC_DIR.rglob("**/components/**/*.css")):
        if fp not in css_order:
            css_order.append(fp)

    parts: list[str] = []
    for fp in css_order:
        try:
            parts.append(f"/* {fp.relative_to(STATIC_DIR)} */\n{fp.read_text(encoding='utf-8')}")
        except Exception:
            pass

    combined = "\n\n".join(parts)
    if minify:
        # Simple minification: collapse whitespace, strip comments.
        import re
        combined = re.sub(r"/\*.*?\*/", "", combined, flags=re.DOTALL)
        combined = re.sub(r"\n\s*\n", "\n", combined)
        combined = re.sub(r"\s+", " ", combined)

    outfile.write_text(combined, encoding="utf-8")
    print(f"[bundle] CSS → {outfile}  ({len(css_order)} files)")


def main() -> None:
    import argparse
    p = argparse.ArgumentParser(description="XMclaw UI bundler")
    p.add_argument("--watch", action="store_true", help="Watch mode (JS only)")
    p.add_argument("--minify", action="store_true", help="Production minified build")
    p.add_argument("--js-only", action="store_true", help="Bundle JS only")
    p.add_argument("--css-only", action="store_true", help="Bundle CSS only")
    args = p.parse_args()

    if not args.css_only:
        bundle_js(minify=args.minify, watch=args.watch)
    if not args.js_only:
        bundle_css(minify=args.minify)

    print("[bundle] Done. Set xmclaw.daemon.static.use_bundles=True to serve from dist/")


if __name__ == "__main__":
    main()
