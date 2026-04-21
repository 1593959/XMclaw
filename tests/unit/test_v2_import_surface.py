"""Smoke: every v2 public-surface module must import cleanly.

If a Phase 1 skeleton file has a syntax or wiring error, this test catches
it before anything else runs.
"""
from __future__ import annotations

import importlib

import pytest

V2_MODULES = [
    "xmclaw.core",
    "xmclaw.core.bus",
    "xmclaw.core.bus.events",
    "xmclaw.core.bus.memory",
    "xmclaw.core.bus.sqlite",
    "xmclaw.core.bus.replay",
    "xmclaw.core.ir",
    "xmclaw.core.ir.toolcall",
    "xmclaw.core.grader",
    "xmclaw.core.grader.verdict",
    "xmclaw.core.grader.checks",
    "xmclaw.core.grader.domain",
    "xmclaw.core.grader.domain.summary",
    "xmclaw.core.scheduler",
    "xmclaw.core.scheduler.online",
    "xmclaw.core.scheduler.policy",
    "xmclaw.core.session",
    "xmclaw.core.session.lifecycle",
    "xmclaw.core.evolution",
    "xmclaw.core.evolution.controller",
    "xmclaw.providers",
    "xmclaw.providers.llm",
    "xmclaw.providers.llm.base",
    "xmclaw.providers.llm.anthropic",
    "xmclaw.providers.llm.openai",
    "xmclaw.providers.llm.translators.anthropic_native",
    "xmclaw.providers.llm.translators.openai_tool_shape",
    "xmclaw.providers.memory",
    "xmclaw.providers.memory.base",
    "xmclaw.providers.memory.sqlite_vec",
    "xmclaw.providers.channel",
    "xmclaw.providers.channel.base",
    "xmclaw.providers.channel.ws",
    "xmclaw.providers.tool",
    "xmclaw.providers.tool.base",
    "xmclaw.providers.tool.builtin",
    "xmclaw.providers.tool.mcp_bridge",
    "xmclaw.providers.runtime",
    "xmclaw.providers.runtime.base",
    "xmclaw.providers.runtime.local",
    "xmclaw.providers.runtime.process",
    "xmclaw.skills",
    "xmclaw.skills.base",
    "xmclaw.skills.manifest",
    "xmclaw.skills.versioning",
    "xmclaw.skills.registry",
    "xmclaw.skills.demo.read_and_summarize",
    "xmclaw.skills.demo.picklable_demo",
    "xmclaw.utils.cost",
    "xmclaw.utils.redact",
    "xmclaw.plugins",
    "xmclaw.plugins.loader",
    "xmclaw.cli.v2",
    "xmclaw.cli.v2_chat",
    "xmclaw.daemon.app_v2",
    "xmclaw.daemon.agent_loop",
    "xmclaw.daemon.factory",
    "xmclaw.daemon.pairing",
]


@pytest.mark.parametrize("module_name", V2_MODULES)
def test_module_imports(module_name: str) -> None:
    importlib.import_module(module_name)
