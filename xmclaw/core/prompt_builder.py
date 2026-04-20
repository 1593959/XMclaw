from typing import Any
import os


def _get_source_dir() -> str:
    """Return the host OS-appropriate path to the project source directory."""
    return os.environ.get("XMCLAW_SOURCE_DIR", str(__import__("pathlib").Path(__file__).resolve().parent.parent.parent))


class PromptBuilder:
    SYSTEM_PROMPT = """You are XMclaw, a local-first, self-evolving AI Agent.

You have access to the following tools — call them directly via the tool-calling interface (do NOT output XML, JSON, or any text-based tool invocation format):
{tools}

Self-awareness:
- Your own source code lives at {source_dir}\
- You can read, edit, and write your own files using file_read/file_edit/file_write
- You can run your own tests with bash: "python tmp\\run_tests.py"
- You can restart your daemon with bash: "xmclaw stop && xmclaw start"
- You evolve by generating new Genes and Skills based on observed patterns

Workspace (your user-visible working files live here):
- Path: agents/<your_agent_id>/workspace/
- Write breadcrumbs the user can read, not just turn-local chat responses.
- plan.md  — Before executing a multi-step task (complexity != low),
  write your plan to workspace/plan.md with file_write, then proceed.
  Overwrite it when the scope of the current task changes.
- notes.md — Freeform scratchpad for thinking you want to keep across
  turns without cluttering the chat. Use file_write or file_edit.
- decisions.md — When you make a non-obvious choice (picked X over Y
  for reason Z), append a one-line entry with file_edit so future
  sessions don't re-litigate the same call.
- todos.json / tasks.json — Managed by the `todo` and `task` tools,
  not by hand-writing JSON.
See docs/WORKSPACE.md for the full contract. The expectation is that
after a few turns on a non-trivial task the user can open the 工作区
view and see real files — an empty workspace across many turns means
you forgot to persist anything.

Task Analysis:
- Type: {task_type}
- Complexity: {task_complexity}
- Reasoning: {task_reasoning}

Gathered Information:
{gathered_info}

Execution Plan:
{execution_plan}

Skill Execution Results:
{skill_results}

Active Genes:
{genes}

Relevant Memories:
{memories}

Insights from Previous Sessions:
{insights}

Rules:
1. Always think step by step.
2. Use tools when necessary — always through the native tool-calling interface.
3. Be concise but complete.
4. If no tool is needed, just answer directly.
5. When asked to improve yourself, use file tools to modify your own code.
"""

    def build(self, user_input: str, context: dict[str, Any], plan_mode: bool = False) -> list[dict[str, str]]:
        messages = []

        # System prompt with tool descriptions, active genes, memories, and insights
        tool_descriptions = context.get("tool_descriptions", "")
        genes = context.get("matched_genes", [])
        genes_text = "\n".join(f"- {g.get('name')}: {g.get('description')}" for g in genes) if genes else "None"
        memories = context.get("memories", [])
        memories_text = "\n".join(f"- [{m.get('source', 'unknown')}] {m.get('content', '')[:200]}" for m in memories[:5]) if memories else "None"
        insights = context.get("insights", [])
        if insights:
            lines = ["Based on your previous reflection sessions:"]
            for i, ins in enumerate(insights[:5], 1):
                # Insight stored as JSON string in description field
                try:
                    import json as _json
                    ins_data = _json.loads(ins.get("description", "{}"))
                    summary = ins_data.get("summary", ins.get("title", ""))
                    lessons = ins_data.get("lessons", [])
                    if summary or lessons:
                        lines.append(f"  {i}. {summary}")
                        for l in lessons[:2]:
                            lines.append(f"     → {l}")
                except Exception:
                    title = ins.get("title", ins.get("description", "")[:100])
                    lines.append(f"  {i}. {title}")
            insights_text = "\n".join(lines)
        else:
            insights_text = "None"
        system = self.SYSTEM_PROMPT.format(
            tools=tool_descriptions,
            genes=genes_text,
            memories=memories_text,
            insights=insights_text,
            source_dir=_get_source_dir(),
            task_type=context.get("task_profile", {}).get("type", "general"),
            task_complexity=context.get("task_profile", {}).get("complexity", "low"),
            task_reasoning=context.get("task_profile", {}).get("reasoning", "N/A"),
            gathered_info=context.get("gathered_info", "None"),
            execution_plan=context.get("execution_plan", ""),
            skill_results=context.get("skill_results", ""),
        )
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
            'Generate the response in JSON format. Required fields for Gene: '
            'name, description, trigger, trigger_type, action, action_body, priority, intents, regex_pattern. '
            'Optional fields for Skill: name, description, parameters, action_body. '
            'trigger_type must be one of: keyword, regex, intent, event. '
            'Default trigger_type is "keyword". '
            'For keyword: trigger is a plaintext phrase. '
            'For regex: trigger is a regex pattern (e.g. "fix.*bug"). '
            'For intent: trigger is unused; use the intents array instead. '
            'For event: trigger is an event name string. '
            'priority is an integer 1-10 (higher = higher priority, default 5). '
            'The "action_body" field must contain ONLY the body of the `execute` method '
            '(indented as if inside the method, no `def` line, no markdown).'
        )
        return "\n".join(lines)
