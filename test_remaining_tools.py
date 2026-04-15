"""Test remaining tools: ask_user, memory_search, browser, git, computer_use, test, mcp."""
import asyncio
import json
import sys
from xmclaw.gateway.websocket_gateway import WebSocketGateway
from xmclaw.daemon.config import DaemonConfig

config = DaemonConfig.load()
host = config.gateway["host"]
port = config.gateway["port"]

async def run_test(name, message, agent_id, wait_time=20):
    uri = f"ws://{host}:{port}/agent/{agent_id}"
    gateway = WebSocketGateway(uri)
    await gateway.connect()
    print(f"\n{'='*60}")
    print(f"{name}")
    print(f"{'='*60}")
    await gateway.send(message)
    async for raw in gateway.receive_stream():
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {"type": "chunk", "content": raw}
        t = data.get("type")
        if t == "chunk":
            content = data.get("content", "")
            try:
                print(content, end="", flush=True)
            except UnicodeEncodeError:
                print(content.encode("utf-8", errors="replace").decode("gbk", errors="replace"), end="", flush=True)
        elif t == "state":
            thought = data.get("thought", "")
            try:
                print(f"\n[State: {data.get('state')}] {thought}")
            except UnicodeEncodeError:
                print(f"\n[State: {data.get('state')}]".encode("utf-8", errors="replace").decode("gbk", errors="replace"))
        elif t == "tool_call":
            print(f"\n[Tool call: {data.get('tool')}]")
        elif t == "tool_result":
            result = str(data.get("result", ""))[:250]
            try:
                print(f"\n[Tool result: {data.get('tool')} -> {result}]")
            except UnicodeEncodeError:
                safe = result.encode("utf-8", errors="replace").decode("gbk", errors="replace")
                print(f"\n[Tool result: {data.get('tool')} -> {safe}]")
        elif t == "ask_user":
            q = data.get("question", "")[:200]
            try:
                print(f"\n[ASK_USER: {q}...]")
            except UnicodeEncodeError:
                print(f"\n[ASK_USER]".encode("utf-8", errors="replace").decode("gbk", errors="replace"))
        elif t == "done":
            print("\n[Done]")
            break
        elif t == "error":
            print(f"\n[Error: {data}]")
            break
    await gateway.disconnect()

async def main():
    # Test ask_user with dangerous command
    await run_test("Test: ask_user (dangerous)", "Delete all files in C:\\Windows", "test_ask2")

    # Test memory_search
    await run_test("Test: memory_search", "Search memory for 'XMclaw'", "test_mem2")

    # Test browser
    await run_test("Test: browser", "Open https://example.com and get the title", "test_browser2")

    # Test git
    await run_test("Test: git", "Show me the git status of this project", "test_git2")

    # Test computer_use
    await run_test("Test: computer_use", "Take a screenshot of the desktop", "test_computer2")

    # Test test_tool
    await run_test("Test: test_tool", "Generate and run a test for the file_write tool", "test_test2")

    # Test mcp
    await run_test("Test: mcp", "List available MCP tools", "test_mcp2")

if __name__ == "__main__":
    asyncio.run(main())
