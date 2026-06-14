"""Minimal MCP echo server — zero dependencies, verifies Proma MCP connectivity."""
import sys, json

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

def write(msg):
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        continue
    method = msg.get("method", "")
    id_ = msg.get("id")

    if method == "initialize":
        write({"jsonrpc":"2.0","id":id_,"result":{"protocolVersion":"2024-11-05","capabilities":{},"serverInfo":{"name":"echo-test","version":"1.0.0"}}})
    elif method == "notifications/initialized":
        pass
    elif method == "tools/list":
        write({"jsonrpc":"2.0","id":id_,"result":{"tools":[{"name":"ping","description":"Test tool","inputSchema":{"type":"object","properties":{}}}]}})
    elif method == "tools/call":
        write({"jsonrpc":"2.0","id":id_,"result":{"content":[{"type":"text","text":"pong"}]}})
