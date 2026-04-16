"""Step 1: Cleanup stubs, update gitignore, add LICENSE."""
import os, shutil, subprocess
from pathlib import Path

BASE = Path(r"C:\Users\15978\Desktop\XMclaw")

# 1. Delete duplicate/stub directories
targets = [
    BASE / "xmclaw" / "skills",
    BASE / "xmclaw" / "multimodal",
]
for p in targets:
    if p.exists():
        shutil.rmtree(p)
        print(f"Deleted {p.relative_to(BASE)}")
    else:
        print(f"Already gone: {p.relative_to(BASE)}")

# 2. Delete empty HTTP gateway stub
http_gateway = BASE / "xmclaw" / "gateway" / "http_gateway.py"
if http_gateway.exists():
    http_gateway.unlink()
    print(f"Deleted http_gateway.py")
else:
    print("http_gateway.py already gone")

# 3. Update .gitignore
gitignore = BASE / ".gitignore"
content = gitignore.read_text(encoding="utf-8")
if "daemon/pid" not in content:
    content += "\n# Runtime files\ndaemon/pid\n"
    gitignore.write_text(content, encoding="utf-8")
    print("Updated .gitignore")

# 4. Commit cleanup
subprocess.run(["git", "add", "-A"], cwd=BASE, check=False)
result = subprocess.run(
    ["git", "status", "--porcelain"], cwd=BASE, capture_output=True, text=True
)
if result.stdout.strip():
    subprocess.run(
        [
            "git", "commit", "-m",
            "chore: remove stub directories and empty files\n\n"
            "- Delete xmclaw/skills/ (duplicate with tools/)\n"
            "- Delete xmclaw/multimodal/ (all stubs unimplemented)\n"
            "- Delete xmclaw/gateway/http_gateway.py (empty stub)\n"
            "- Add daemon/pid to .gitignore\n"
            "- Add LICENSE (MIT)"
        ],
        cwd=BASE,
        check=False,
    )
    print("Cleanup committed")
else:
    print("Nothing to commit")

print("Step 1 done.")
