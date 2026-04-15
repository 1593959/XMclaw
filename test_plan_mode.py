"""Test plan mode with confirmation."""
import asyncio
import json
from xmclaw.gateway.websocket_gateway import WebSocketGateway
from xmclaw.daemon.config import DaemonConfig

config = DaemonConfig.load()
host = config.gateway["host"]
port = config.gateway["port"]
uri = f"ws://{host}:{port}/agent/test_plan"

async def main():
    gateway = WebSocketGateway(uri)
    await gateway.connect()
    print("Connected!")

    # Send plan mode request
    print("\n=== Sending plan mode request ===")
    await gateway.send("[PLAN MODE] Write a hello world Python script")

    pending_question = None
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
            print(f"\n[Tool call: {data.get('tool')}]")
        elif t == "tool_result":
            print(f"\n[Tool result: {data.get('tool')} -> {str(data.get('result', ''))[:100]}]")
        elif t == "ask_user":
            pending_question = data.get("question", "")
            print(f"\n[ASK_USER: {pending_question[:200]}...]")
            break
        elif t == "done":
            print("\n[Done]")
            break
        elif t == "error":
            print(f"\n[Error: {data}]")
            break

    if pending_question:
        print("\n=== Sending confirmation ===")
        await gateway.send("[RESUME] Yes, execute the plan")
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
                print(f"\n[Tool call: {data.get('tool')}]")
            elif t == "tool_result":
                print(f"\n[Tool result: {data.get('tool')} -> {str(data.get('result', ''))[:100]}]")
            elif t == "done":
                print("\n[Done]")
                break
            elif t == "error":
                print(f"\n[Error: {data}]")
                break

    await gateway.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
