"""CLI client that connects to the daemon via WebSocket."""
import json
import asyncio
from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout
from xmclaw.cli.rich_ui import print_banner, print_user, print_agent, print_tool
from xmclaw.gateway.websocket_gateway import WebSocketGateway
from xmclaw.daemon.config import DaemonConfig


async def run_cli_client(agent_id: str):
    config = DaemonConfig.load()
    host = config.gateway["host"]
    port = config.gateway["port"]
    uri = f"ws://{host}:{port}/agent/{agent_id}"

    gateway = WebSocketGateway(uri)
    await gateway.connect()
    print_banner()

    session = PromptSession()
    try:
        while True:
            with patch_stdout():
                user_input = await session.prompt_async("You: ")
            if user_input.strip() in ("/quit", "/exit"):
                break

            print_user(user_input)
            await gateway.send(user_input)

            response = ""
            async for chunk in gateway.receive_stream():
                if chunk.startswith("[Tool:"):
                    print_tool("tool", chunk)
                else:
                    response += chunk

            if response.strip():
                print_agent(response.strip())
    except KeyboardInterrupt:
        pass
    finally:
        await gateway.disconnect()
