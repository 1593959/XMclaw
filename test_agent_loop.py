"""Test script for AgentLoop basic conversation."""
import asyncio
import json
from xmclaw.gateway.websocket_gateway import WebSocketGateway
from xmclaw.daemon.config import DaemonConfig

async def test_basic_chat():
    config = DaemonConfig.load()
    host = config.gateway["host"]
    port = config.gateway["port"]
    uri = f"ws://{host}:{port}/agent/test_agent"

    gateway = WebSocketGateway(uri)
    print(f"Connecting to {uri}...")
    await gateway.connect()
    print("Connected!")

    events = []

    async def run_test(name, message, wait_time=10):
        print(f"\n{'='*50}")
        print(f"{name}")
        print(f"{'='*50}")
        await gateway.send(message)
        end_time = asyncio.get_event_loop().time() + wait_time
        async for raw in gateway.receive_stream():
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = {"type": "chunk", "content": raw}
            events.append(data)
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
            if asyncio.get_event_loop().time() >= end_time:
                print("\n[Timeout]")
                break

    await run_test("Test 1: Simple greeting", "Hello, what is your name?", 8)
    await run_test("Test 2: Todo tool", "Add a todo: test XMclaw agent loop", 12)
    await run_test("Test 3: File operation", "Write a file called test_hello.txt with content 'hello from XMclaw'", 12)

    await gateway.disconnect()

    with open("test_agent_loop_events.json", "w", encoding="utf-8") as f:
        json.dump(events, f, ensure_ascii=False, indent=2)
    print("\n\nEvents saved to test_agent_loop_events.json")

if __name__ == "__main__":
    asyncio.run(test_basic_chat())
