"""Test script for AgentLoop - separate connections per test."""
import asyncio
import json
from xmclaw.gateway.websocket_gateway import WebSocketGateway
from xmclaw.daemon.config import DaemonConfig

config = DaemonConfig.load()
host = config.gateway["host"]
port = config.gateway["port"]

async def run_single_test(name, message, agent_id, wait_time=15):
    uri = f"ws://{host}:{port}/agent/{agent_id}"
    gateway = WebSocketGateway(uri)
    await gateway.connect()
    print(f"\n{'='*50}")
    print(f"{name}")
    print(f"{'='*50}")
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
        elif t == "done":
            print("\n[Done]")
            break
        elif t == "error":
            print(f"\n[Error: {data}]")
            break
    await gateway.disconnect()

async def main():
    await run_single_test("Test 1: Simple greeting", "Hello, what is your name?", "test1")
    await run_single_test("Test 2: Todo tool", "Add a todo: test XMclaw agent loop", "test2")
    await run_single_test("Test 3: File operation", "Write a file called test_hello.txt with content 'hello from XMclaw'", "test3")
    await run_single_test("Test 4: Bash tool", "Run 'python --version' in bash", "test4")
    await run_single_test("Test 5: Web search", "Search for Python asyncio best practices", "test5")

if __name__ == "__main__":
    asyncio.run(main())
