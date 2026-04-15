"""Prompt builder for LLM interactions."""
from typing import Any


class PromptBuilder:
    SYSTEM_PROMPT = """You are XMclaw, a local-first, self-evolving AI Agent.
You have access to tools. When you need to use a tool, output in this exact format:

<function>tool_name</function>
<arguments>
{"key": "value"}
</arguments>

Available tools:
{tools}

Self-awareness:
- Your own source code lives at C:\Users\15978\Desktop\XMclaw\
- You can read, edit, and write your own files using file_read/file_edit/file_write
- You can run your own tests with bash: "python tmp\\run_tests.py"
- You can restart your daemon with bash: "xmclaw stop && xmclaw start"
- You evolve by generating new Genes and Skills based on observed patterns

Rules:
1. Always think step by step.
2. Use tools when necessary.
3. Be concise but complete.
4. If no tool is needed, just answer directly.
5. When asked to improve yourself, use file tools to modify your own code.
"""

    def build(self, user_input: str, context: dict[str, Any]) -> list[dict[str, str]]:
        messages = []

        # System prompt with tool descriptions
        tool_descriptions = context.get("tool_descriptions", "")
        system = self.SYSTEM_PROMPT.format(tools=tool_descriptions)
        messages.append({"role": "system", "content": system})

        # Recent conversation history
        history = context.get("history", [])
        for turn in history[-10:]:  # Keep last 10 turns
            messages.append({"role": "user", "content": turn.get("user", "")})
            messages.append({"role": "assistant", "content": turn.get("assistant", "")})

        # Current user input
        messages.append({"role": "user", "content": user_input})
        return messages

    def build_evolution_prompt(self, insights: list[dict]) -> str:
        lines = [
            "Based on the following user behavior insights, generate a new Gene (behavior rule):",
            "",
        ]
        for i, insight in enumerate(insights, 1):
            lines.append(f"{i}. {insight.get('title')}: {insight.get('description')}")
        lines.append("")
        lines.append("Generate a Gene in JSON format with fields: id, name, description, trigger, action")
        return "\n".join(lines)
