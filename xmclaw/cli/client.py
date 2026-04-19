"""CLI client that connects to the daemon via WebSocket."""
import json
from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout
from xmclaw.cli.rich_ui import (
    print_banner, print_user, print_agent, print_tool, print_state, print_ask_user
)
from xmclaw.gateway.websocket_gateway import WebSocketGateway
from xmclaw.daemon.config import DaemonConfig


def _process_chunk(data: dict, response_buffer: list[str]) -> str:
    """Handle a single parsed WS message. Returns msg_type."""
    msg_type = data.get("type", "chunk")
    if msg_type == "chunk":
        content = data.get("content", "")
        response_buffer[0] += content
        print_agent(content, end="", flush=True)
    elif msg_type == "state":
        state = data.get("state", "")
        thought = data.get("thought", "")
        print_state(state, thought)
    elif msg_type == "tool_result":
        tool = data.get("tool", "")
        result = data.get("result", "")
        print_tool(tool, result)
    elif msg_type == "ask_user":
        question = data.get("question", "")
        print_ask_user(question)
    elif msg_type == "reflection":
        from xmclaw.cli.rich_ui import print_reflection
        print_reflection(data.get("data", {}))
    elif msg_type == "done":
        if response_buffer[0] and not response_buffer[0].endswith("\n"):
            print()
        response_buffer[0] = ""
    elif msg_type == "error":
        print_agent(f"[Error: {data.get('content', '')}]")
    return msg_type


async def _handle_stream(gateway: WebSocketGateway, session: PromptSession) -> None:
    """Consume the WebSocket stream until done/error or ask_user (paused for user input).

    When ask_user is encountered the stream is paused, the user is prompted,
    the answer is sent back via asend(), and this function calls itself
    recursively to consume the resumed stream.  The recursive call processes
    everything up to and including the matching 'done' event, at which point
    the outer `async for` loop also sees StopAsyncIteration and exits cleanly.
    """
    response_buffer = [""]

    async for raw in gateway.receive_stream():
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {"type": "chunk", "content": raw}

        msg_type = _process_chunk(data, response_buffer)

        if msg_type == "ask_user":
            question = data.get("question", "")
            print_ask_user(question)
            with patch_stdout():
                answer = await session.prompt_async("Answer: ")
            response_buffer[0] = ""  # reset buffer for resumed stream
            await gateway.send_ask_user_answer(answer)
            # Resume: the server's async generator continues from its yield point
            # and sends the remaining messages through the same WS socket.
            # Recursive call processes them until done/error.
            await _handle_stream(gateway, session)
            return  # resume chain: outer async-for sees StopAsyncIteration

        if msg_type in ("done", "error"):
            return  # stream iterator will close naturally


async def run_cli_client(agent_id: str, plan_mode: bool = False):
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
            if plan_mode and not user_input.startswith("[PLAN MODE]"):
                user_input = f"[PLAN MODE] {user_input}"
            await gateway.send(user_input)
            await _handle_stream(gateway, session)
    except KeyboardInterrupt:
        pass
    finally:
        await gateway.disconnect()
