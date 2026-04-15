"""Test advanced features: plan mode, ask_user, memory_search, browser, git."""
import asyncio
import json
from xmclaw.gateway.websocket_gateway import WebSocketGateway
from xmclaw.daemon.config import DaemonConfig

config = DaemonConfig.load()
host = config.gateway["host"]
port = config.gateway["port"]

async def run_single_test(name, message, agent_id, wait_time=20):
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
            print(data.get("content", ""), end="", flush=True)
        elif t == "state":
            print(f"\n[State: {data.get('state')}] {data.get('thought', '')}")
        elif t == "tool_call":
            print(f"\n[Tool call: {data.get('tool')} args={data.get('args', {})}]")
        elif t == "tool_result":
            result = str(data.get("result", ""))[:200]
            print(f"\n[Tool result: {data.get('tool')} -> {result}]")
        elif t == "ask_user":
            print(f"\n[ASK_USER: {data.get('question', '')}]")
        elif t == "done":
            print("\n[Done]")
            break
        elif t == "error":
            print(f"\n[Error: {data}]")
            break
    await gateway.disconnect()

async def main():
    # Test 6: Plan mode
    await run_single_test(
        "Test 6: Plan mode",
        "[PLAN MODE] Create a Python script that fetches weather data and prints it",
        "test6"
    )

    # Test 7: ask_user
    await run_single_test(
        "Test 7: ask_user (dangerous bash)",
        "Delete all files in the current directory",
        "test7"
    )

    # Test 8: memory_search
    await run_single_test(
        "Test 8: memory_search",
        "Search my memory for 'XMclaw' and tell me what you find",
        "test8"
    )

    # Test 9: browser
    await run_single_test(
        "Test 9: browser",
        "Open https://github.com in the browser and tell me the page title",
        "test9"
    )

    # Test 10: git
    await run_single_test(
        "Test 10: git",
        "Run git status in this repository",
        "test10"
    )

if __name__ == "__main__":
    asyncio.run(main())
