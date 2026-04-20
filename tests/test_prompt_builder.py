"""Regression guards for the system prompt.

The agent will only touch the workspace if the prompt actually tells it
to. These tests lock in the workspace-use instructions so a future
prompt refactor can't silently drop them and quietly reintroduce the
"many conversation rounds, zero workspace files created" bug (F/H).
"""
import pytest
from xmclaw.core.prompt_builder import PromptBuilder


def _render(**ctx):
    pb = PromptBuilder()
    base_ctx = {
        "tool_descriptions": "",
        "matched_genes": [],
        "memories": [],
        "insights": [],
        "task_profile": {"type": "general", "complexity": "low", "reasoning": "N/A"},
        "gathered_info": "None",
        "execution_plan": "",
        "skill_results": "",
        "history": [],
    }
    base_ctx.update(ctx)
    msgs = pb.build("hello", base_ctx)
    return next(m["content"] for m in msgs if m["role"] == "system")


def test_system_prompt_mentions_workspace_path():
    """Agent must know WHERE the workspace is, or it won't write there."""
    prompt = _render()
    assert "agents/" in prompt and "workspace" in prompt


def test_system_prompt_instructs_plan_md_for_multi_step_tasks():
    """plan.md exists so the user can redirect before work starts."""
    prompt = _render()
    assert "plan.md" in prompt


def test_system_prompt_instructs_decisions_md_for_non_obvious_choices():
    """decisions.md leaves breadcrumbs across sessions."""
    prompt = _render()
    assert "decisions.md" in prompt


def test_system_prompt_references_workspace_docs():
    """Pointer to the full contract so the agent can look it up."""
    prompt = _render()
    assert "WORKSPACE.md" in prompt or "docs/WORKSPACE" in prompt
