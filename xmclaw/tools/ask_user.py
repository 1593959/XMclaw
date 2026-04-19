"""Ask user question tool - pauses execution for user input."""
from xmclaw.tools.base import Tool


class AskUserTool(Tool):
    name = "ask_user"
    description = "Ask the user a clarifying question and wait for their answer. Use this when you need more information to proceed."
    parameters = {
        "question": {
            "type": "string",
            "description": "The question to ask the user.",
        },
    }

    async def execute(self, question: str) -> str:
        # This tool is handled specially by the orchestrator.
        # The orchestrator detects this tool call and pauses the loop,
        # sending a special event to the UI to prompt the user.
        # When the user replies, the answer is injected back as a tool result.
        return f"[ASK_USER] {question}"
