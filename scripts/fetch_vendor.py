#!/usr/bin/env python3
"""Fetch the vendored Preact + htm bundles for the XMclaw Web UI.

The bootstrap (xmclaw/daemon/static/bootstrap.js) tries the ``esm.sh``
CDN first and falls back to these local copies. Offline-only installs,
or users behind a restrictive proxy, depend on this script running at
least once.

Usage::

    python scripts/fetch_vendor.py             # default URLs
    python scripts/fetch_vendor.py --force     # overwrite existing
    python scripts/fetch_vendor.py --check     # don't download, just
                                               # report which files
                                               # are present

See docs/FRONTEND_DESIGN.md §11.3.1 (ADR-009).
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "xmclaw" / "daemon" / "static" / "vendor"

# The files live at stable esm.sh paths; we pin the major version so a
# surprise major-version bump on the CDN doesn't leak into the vendor
# fallback.
SOURCES: dict[str, str] = {
    "preact.min.js": "https://esm.sh/preact@10?target=es2020",
    "htm.min.js": "https://esm.sh/htm@3?target=es2020",
}


def _download(url: str, dest: Path) -> tuple[int, str]:
    """Download ``url`` into ``dest``. Returns (bytes_written, sha256)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310
        data = resp.read()
    dest.write_bytes(data)
    return len(data), hashlib.sha256(data).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing vendor files instead of skipping them.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Don't download — just report which vendor files are present.",
    )
    args = parser.parse_args()

    VENDOR.mkdir(parents=True, exist_ok=True)

    if args.check:
        missing = 0
        for name in SOURCES:
            dest = VENDOR / name
            if dest.exists():
                size = dest.stat().st_size
                print(f"ok   {name}  ({size} bytes)")
            else:
                missing += 1
                print(f"miss {name}")
        if missing:
            print(
                f"\n{missing} vendor file(s) missing. Run "
                f"`python scripts/fetch_vendor.py` to populate."
            )
            return 1
        return 0

    errors = 0
    for name, url in SOURCES.items():
        dest = VENDOR / name
        if dest.exists() and not args.force:
            print(f"skip {name}  (already present; use --force to redownload)")
            continue
        try:
            size, digest = _download(url, dest)
        except Exception as exc:  # noqa: BLE001
            errors += 1
            print(f"fail {name}  {exc}", file=sys.stderr)
            continue
        print(f"ok   {name}  {size} bytes  sha256={digest[:12]}…")

    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
