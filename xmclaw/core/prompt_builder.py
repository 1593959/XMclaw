"""Prompt builder for LLM interactions."""
from typing import Any


class PromptBuilder:
    SYSTEM_PROMPT = """You are XMclaw, a local-first, self-evolving AI Agent.

You have access to the following tools — call them directly via the tool-calling interface (do NOT output XML, JSON, or any text-based tool invocation format):
{tools}

Self-awareness:
- Your own source code lives at C:\\Users\\15978\\Desktop\\XMclaw\\
- You can read, edit, and write your own files using file_read/file_edit/file_write
- You can run your own tests with bash: "python tmp\\run_tests.py"
- You can restart your daemon with bash: "xmclaw stop && xmclaw start"
- You evolve by generating new Genes and Skills based on observed patterns

Active Genes:
{genes}

Relevant Memories:
{memories}

Rules:
1. Always think step by step.
2. Use tools when necessary — always through the native tool-calling interface.
3. Be concise but complete.
4. If no tool is needed, just answer directly.
5. When asked to improve yourself, use file tools to modify your own code.
"""

    PLAN_MODE_PROMPT = """You are in PLAN MODE. Do not execute tools yet.
Instead, analyze the request and produce a step-by-step execution plan.

Your plan should include:
1. What information you need to gather
2. What tools you will use and in what order
3. What files you might need to read or modify
4. Any potential risks or edge cases

After producing the plan, ask the user if they want you to proceed with execution.
Use the ask_user tool to confirm before taking action.
"""

    def build(self, user_input: str, context: dict[str, Any], plan_mode: bool = False) -> list[dict[str, str]]:
        messages = []

        # System prompt with tool descriptions, active genes, and memories
        tool_descriptions = context.get("tool_descriptions", "")
        genes = context.get("matched_genes", [])
        genes_text = "\n".join(f"- {g.get('name')}: {g.get('description')}" for g in genes) if genes else "None"
        memories = context.get("memories", [])
        memories_text = "\n".join(f"- [{m.get('source', 'unknown')}] {m.get('content', '')[:200]}" for m in memories[:5]) if memories else "None"
        system = self.SYSTEM_PROMPT.format(tools=tool_descriptions, genes=genes_text, memories=memories_text)
        if plan_mode:
            system += "\n\n" + self.PLAN_MODE_PROMPT
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
            "Based on the following user behavior insights, generate a new Gene or Skill:",
            "",
        ]
        for i, insight in enumerate(insights, 1):
            lines.append(f"{i}. {insight.get('title')}: {insight.get('description')}")
        lines.append("")
        lines.append(
            'Generate the response in JSON format with fields: '
            'name, description, trigger, action, action_body. '
            'The "action_body" field must contain ONLY the body of the `execute` method '
            '(indented as if inside the method, no `def` line, no markdown).'
        )
        return "\n".join(lines)
