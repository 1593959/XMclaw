from typing import Any
import os


def _get_source_dir() -> str:
    """Return the host OS-appropriate path to the project source directory."""
    return os.environ.get("XMCLAW_SOURCE_DIR", str(__import__("pathlib").Path(__file__).resolve().parent.parent.parent))


class PromptBuilder:
    PLAN_MODE_PROMPT = """[PLAN MODE is ON]
The user wants to see the plan before you execute.
- Do NOT call side-effect tools (file_write, file_edit, bash, code_exec, git, github).
- Read-only tools (file_read, grep, glob, web_fetch, web_search, memory_search) are fine for gathering info.
- End your response with a numbered plan and ask the user to approve before you act.
- Once the user approves in a later turn, execute normally."""

    SYSTEM_PROMPT = """You are XMclaw, a local-first, self-evolving AI Agent.
{identity}
You have access to the following tools — call them directly via the tool-calling interface (do NOT output XML, JSON, or any text-based tool invocation format):
{tools}

Self-awareness:
- Your own source code lives at {source_dir}\
- You can read, edit, and write your own files using file_read/file_edit/file_write
- You can run your own tests with bash: "python -m pytest tests/"
- You can restart your daemon with bash: "xmclaw restart"
- You evolve by generating new Genes and Skills based on observed patterns

Workspace (the user's window onto who you are + what you're doing):
- Agent dir: agents/<your_agent_id>/
- Identity (read every turn, edit on user request):
    SOUL.md    — your personality, values, voice
    PROFILE.md — who the user is, their preferences, domain knowledge
    AGENTS.md  — multi-agent team roster + delegation rules
  If the user says "remember I prefer X" or "you should be more Y",
  that usually belongs in PROFILE.md or SOUL.md, not in chat.
- Task-level scratch (you own these; write breadcrumbs the user can
  read, not just turn-local chat responses):
    workspace/plan.md      — Before executing a multi-step task
      (complexity != low), write your plan here with file_write,
      then proceed. Overwrite when the scope of the task changes.
    workspace/notes.md     — Freeform scratchpad for cross-turn
      thinking that shouldn't clutter the chat.
    workspace/decisions.md — Append-only log of non-obvious choices
      ("picked X over Y because Z") so future sessions don't
      re-litigate the same call. Use file_edit with mode="append".
    workspace/todos.json   — Managed by the `todo` tool, not hand-written.
    workspace/tasks.json   — Managed by the `task` tool, not hand-written.
- Never touch (the daemon blocks these anyway):
    agent.json    — API keys
    memory/       — SQLite DB + session logs; corrupts if edited
See docs/WORKSPACE.md for the full contract. An empty workspace/
after many turns on a non-trivial task means you forgot to persist
breadcrumbs — fix it next turn.

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

CRITICAL — never fabricate tool output (this is the #1 way we lose user trust):
- Side-effect tools (file_write, file_edit, bash, code_exec, git, github, shell)
  MUST be actually invoked through the native tool-calling interface. Do NOT
  emit phrases like "✅ 已写入", "已完成", "**输出:**", "```diff …```",
  "running…", or any stdout/stderr transcript unless a real tool_result for
  that exact call is visible to you in this turn. The user's filesystem, git
  history, and shell are the source of truth — a hallucinated "success"
  message is strictly worse than asking, because it makes the user believe
  work happened that didn't.
- "I will now run X" followed by a fake transcript is the forbidden pattern.
  Either invoke the tool (preferred) or say plainly "I haven't run X yet —
  want me to?" and stop. Never both describe an action AND fabricate its
  result in the same turn.
- Read tools (file_read, grep, glob, web_fetch, web_search, memory_search):
  when the user asks you to look something up in the repo or on the web,
  INVOKE the tool even when you think you already know the answer. Your
  training data is stale; the repo may have changed since; the web is
  always newer than you. Quoting file contents or command stdout without a
  preceding tool_call for that exact content is a bug, not a shortcut.
- If a tool errors, is unavailable, or you aren't sure whether it ran: say
  so explicitly ("web_search returned [No results found]", "file_read failed
  with PermissionError"). Do not paper over with plausible-looking fake
  output. Honest "I don't know" beats confident lies every time.
- After every side-effect, the next thing you say to the user should match
  reality. If you see no tool_result for a write, assume the write did NOT
  happen — re-invoke the tool rather than claiming it did.
"""

    @staticmethod
    def _format_identity(soul: str, profile: str, agents: str = "", workspace: str = "") -> str:
        """Render SOUL.md + PROFILE.md + AGENTS.md + workspace breadcrumbs into the system prompt.

        These files are the agent's persona, the user's context, the
        multi-agent roster, and the agent's own cross-turn scratch. They
        used to be loaded into memory but never passed to the LLM — the
        LLM had no idea who it was, who the user was, what teammates
        existed, or what it wrote to plan.md on the previous turn.
        A 4k-char cap per file keeps worst-case token cost bounded.
        """
        soul = (soul or "").strip()
        profile = (profile or "").strip()
        agents = (agents or "").strip()
        workspace = (workspace or "").strip()
        if not any([soul, profile, agents, workspace]):
            return ""
        CAP = 4000
        blocks = ["\n"]
        if soul:
            blocks.append("── Your identity (from SOUL.md) ──")
            blocks.append(soul[:CAP] + ("\n…(truncated)" if len(soul) > CAP else ""))
        if profile:
            blocks.append("── The user you are talking to (from PROFILE.md) ──")
            blocks.append(profile[:CAP] + ("\n…(truncated)" if len(profile) > CAP else ""))
        if agents:
            blocks.append("── Your multi-agent team (from AGENTS.md) ──")
            blocks.append(agents[:CAP] + ("\n…(truncated)" if len(agents) > CAP else ""))
        if workspace:
            blocks.append("── Your cross-turn scratch (from workspace/) ──")
            blocks.append(workspace[:CAP] + ("\n…(truncated)" if len(workspace) > CAP else ""))
            blocks.append(
                "These are breadcrumbs YOU wrote in previous turns. Consult them before "
                "asking the user to repeat themselves; update them when the plan shifts."
            )
        blocks.append("Honor SOUL.md in voice and values. Use PROFILE.md to tailor answers to this specific user.\n")
        return "\n".join(blocks)

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
        identity_text = self._format_identity(
            context.get("soul", ""),
            context.get("profile", ""),
            context.get("agents", ""),
            context.get("workspace_breadcrumbs", ""),
        )
        system = self.SYSTEM_PROMPT.format(
            identity=identity_text,
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
