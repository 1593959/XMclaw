"""One-shot fetch of vis-network into ``xmclaw/daemon/static/vendor/``.

Background
==========

The Memory ▸ L1 Facts ▸ Graph view renders a force-directed graph
of facts + relations via vis-network. The runtime loader
(``memory_facts_v2_graph.js``) tries paths in this order:

  1. ``/ui/vendor/vis-network.min.js``  ← local, instant
  2. ``https://esm.sh/...``             ← CDN, ~250 KB + RTT
  3. ``https://cdn.jsdelivr.net/...``
  4. ``https://unpkg.com/...``

On a fresh clone the vendor file is missing (``vendor/.gitkeep``
only) so every user pays the CDN cost on first graph open. In
China the CDNs can be slow / blocked entirely; the user reported
the 250 KB pull taking long enough to be a UX problem.

Running this script downloads the file once and parks it under
the static dir. After that the loader's first-attempt local
path 200s and the CDN attempt never fires.

Usage
=====

    python scripts/vendor_vis_network.py

Idempotent — re-running with the file already present is a no-op
unless ``--force`` is passed. Network-only failures print a
clear hint; no destructive operations.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.request
from pathlib import Path


# 9.1.9 is the version the loader pins; bump in lockstep with
# memory_facts_v2_graph.js if upgrading.
_VERSION = "9.1.9"
_SOURCES = (
    f"https://cdn.jsdelivr.net/npm/vis-network@{_VERSION}/standalone/umd/vis-network.min.js",
    f"https://unpkg.com/vis-network@{_VERSION}/standalone/umd/vis-network.min.js",
    f"https://esm.sh/vis-network@{_VERSION}",
)


def _project_static_root() -> Path:
    here = Path(__file__).resolve()
    return here.parent.parent / "xmclaw" / "daemon" / "static"


def _target_path() -> Path:
    return _project_static_root() / "vendor" / "vis-network.min.js"


def _try_fetch(url: str, timeout_s: float = 15.0) -> bytes | None:
    """Best-effort fetch. Returns the body on success, None on failure
    (prints a one-liner). Never raises."""
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "xmclaw-vendor-fetch/1.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = resp.read()
            if not data:
                print(f"  · {url}: empty body", flush=True)
                return None
            if len(data) < 50_000:
                # The UMD bundle is ~250 KB minified. Anything tiny
                # is almost certainly an HTML error page from a CDN
                # rate-limit / regional block.
                print(
                    f"  · {url}: suspiciously small ({len(data)} B) — "
                    f"probably an error page, skipping",
                    flush=True,
                )
                return None
            return data
    except Exception as exc:  # noqa: BLE001
        print(f"  · {url}: {type(exc).__name__}: {exc}", flush=True)
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force", action="store_true",
        help="Re-download even when the file already exists.",
    )
    args = parser.parse_args()

    dest = _target_path()
    if dest.exists() and not args.force:
        size = dest.stat().st_size
        print(
            f"vis-network already vendored at {dest}\n"
            f"  size: {size:,} bytes\n"
            f"  pass --force to re-download",
            flush=True,
        )
        return 0

    dest.parent.mkdir(parents=True, exist_ok=True)
    print(
        f"fetching vis-network {_VERSION} → {dest}",
        flush=True,
    )
    for url in _SOURCES:
        print(f"trying {url} ...", flush=True)
        body = _try_fetch(url)
        if body is not None:
            dest.write_bytes(body)
            sha = hashlib.sha256(body).hexdigest()[:16]
            print(
                f"\n[ok] wrote {len(body):,} bytes "
                f"to {dest}\n     sha256 (first 16): {sha}",
                flush=True,
            )
            # ASCII-only output here — Windows gbk console crashes on
            # the unicode triangle arrow.
            print(
                "\nRefresh the browser; the Memory > L1 Facts > Graph\n"
                "view will now load instantly without a CDN round-trip.",
                flush=True,
            )
            return 0

    print(
        "\n[fail] could not reach any CDN source. "
        "Check network / proxy / firewall. You can also manually "
        f"download vis-network {_VERSION} standalone UMD and drop "
        f"it at:\n  {dest}",
        flush=True,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
