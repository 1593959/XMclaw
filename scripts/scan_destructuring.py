"""One-shot scan: Promise.all destructuring count mismatch.

Catches the exact off-by-one pattern that bit Cognition.js
(``const [s, t, p, g, tg, dh] = await Promise.all([...7 items...])``).
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGETS = (
    list((ROOT / "xmclaw/daemon/static/pages").glob("*.js"))
    + list((ROOT / "xmclaw/daemon/static/pages/_panels").glob("*.js"))
    + list((ROOT / "xmclaw/daemon/static/lib").glob("*.js"))
    + list((ROOT / "xmclaw/daemon/static/components").rglob("*.js"))
    + [ROOT / "xmclaw/daemon/static/app.js"]
)

PAT = re.compile(
    r"const\s*\[([^\]]+)\]\s*=\s*await\s+Promise\.all\s*\(\s*\[(.+?)\]\s*\)",
    re.DOTALL,
)


def count_top_level_items(body: str) -> int:
    depth = 0
    items = 1
    in_str = None
    i = 0
    while i < len(body):
        c = body[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == in_str:
                in_str = None
        elif c in ('"', "'", "`"):
            in_str = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "," and depth == 0:
            items += 1
        i += 1
    # Trailing comma compensation
    stripped = body.rstrip()
    if stripped.endswith(","):
        items -= 1
    if not stripped:
        return 0
    return items


bad = 0
for f in TARGETS:
    if not f.exists():
        continue
    try:
        src = f.read_text(encoding="utf-8")
    except Exception:
        continue
    for m in PAT.finditer(src):
        names = [n.strip() for n in m.group(1).split(",") if n.strip()]
        items = count_top_level_items(m.group(2))
        line = src[: m.start()].count("\n") + 1
        if items != len(names):
            rel = f.relative_to(ROOT)
            print(
                f"MISMATCH {rel}:{line}  destructured={len(names)} "
                f"promise.all items={items}"
            )
            print(f"  names: {names}")
            bad += 1

print(f"\n=== scan complete: {bad} mismatch(es) ===")
sys.exit(1 if bad else 0)
