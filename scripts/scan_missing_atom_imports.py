"""Scan UI pages for atom/component identifiers used inside ${...} or <${X}>
references but never imported. Catches the exact "Skeleton is not defined"
pattern that bit Skills.js."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGETS = (
    list((ROOT / "xmclaw/daemon/static/pages").glob("*.js"))
    + list((ROOT / "xmclaw/daemon/static/pages/_panels").glob("*.js"))
    + list((ROOT / "xmclaw/daemon/static/components").rglob("*.js"))
    + [ROOT / "xmclaw/daemon/static/app.js"]
)

# Heuristic: identifiers used as <${Name}> or ${Name} that look like
# CamelCase components (PascalCase, starts upper, no dot). Require leading
# uppercase letter so we skip ${5} numeric interpolations.
USE_PAT = re.compile(r"<\$\{([A-Z]\w+)\}|\$\{([A-Z]\w+)\s*[\}\s]")
IMPORT_NAME_PAT = re.compile(r"import\s+\{[^}]+\}\s+from")
NAMED_IMPORT_PAT = re.compile(r"\b(\w+)(?:\s+as\s+\w+)?\s*[,}]")

# Identifiers that are commonly LOCAL (defined in the file or are JS
# globals / hooks). We skip them — only PascalCase atom/component imports
# matter here.
SKIP_LOWER = re.compile(r"^[a-z]")
JS_GLOBALS = {
    "Math", "Date", "Object", "Array", "JSON", "RegExp", "Number",
    "String", "Boolean", "Map", "Set", "Promise", "Error",
    "NaN", "Infinity", "URL", "URLSearchParams",
}


def imported_names(src: str) -> set[str]:
    names: set[str] = set()
    for m in re.finditer(
        r"import\s+\{([^}]+)\}\s+from", src,
    ):
        for raw in m.group(1).split(","):
            n = raw.strip().split(" as ")[-1].strip()
            if n:
                names.add(n)
    # also default-import: ``import Foo from "..."``
    for m in re.finditer(r"import\s+(\w+)\s+from", src):
        names.add(m.group(1))
    # destructured const from globals: ``const { h, render } = window.__xmc.preact``
    for m in re.finditer(
        r"const\s*\{([^}]+)\}\s*=\s*window", src,
    ):
        for raw in m.group(1).split(","):
            n = raw.strip()
            if n:
                names.add(n)
    return names


def declared_names(src: str) -> set[str]:
    names: set[str] = set()
    # function declarations
    for m in re.finditer(r"function\s+(\w+)", src):
        names.add(m.group(1))
    # const / let / var top-level-ish
    for m in re.finditer(
        r"^(?:export\s+)?(?:const|let|var)\s+(\w+)", src, re.MULTILINE,
    ):
        names.add(m.group(1))
    # class declarations
    for m in re.finditer(r"class\s+(\w+)", src):
        names.add(m.group(1))
    return names


bad = 0
for f in TARGETS:
    try:
        src = f.read_text(encoding="utf-8")
    except Exception:
        continue
    available = imported_names(src) | declared_names(src) | JS_GLOBALS
    seen: set[tuple[str, int]] = set()
    for m in USE_PAT.finditer(src):
        name = m.group(1) or m.group(2)
        if not name:
            continue
        if SKIP_LOWER.match(name):
            continue
        if name in available:
            continue
        line = src[: m.start()].count("\n") + 1
        key = (name, line)
        if key in seen:
            continue
        seen.add(key)
        rel = f.relative_to(ROOT)
        print(f"MISSING {rel}:{line}  uses ${{${name}}} but no import/decl")
        bad += 1

print(f"\n=== scan complete: {bad} suspect(s) ===")
sys.exit(1 if bad else 0)
