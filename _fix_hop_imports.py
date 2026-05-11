with open('xmclaw/daemon/hop_loop.py', 'r', encoding='utf-8') as f:
    content = f.read()

old_header = '"""Hop loop mixin for AgentLoop.\n\nExtracted from agent_loop.py to reduce module size.\nContains the LLM ↔ tool hop loop execution logic.\n"""\nfrom __future__ import annotations\n\nimport asyncio\nimport time\nfrom typing import Any\n\nfrom xmclaw.core.bus import EventType\nfrom xmclaw.providers.llm.base import Message\n\n\nclass HopLoopMixin:\n    """Provides the LLM ↔ tool hop loop."""'

new_header = '"""Hop loop mixin for AgentLoop.\n\nExtracted from agent_loop.py to reduce module size.\nContains the LLM ↔ tool hop loop execution logic.\n"""\nfrom __future__ import annotations\n\nimport asyncio\nimport time\nfrom collections.abc import Awaitable, Callable\nfrom typing import Any\n\nfrom xmclaw.core.bus import BehavioralEvent, EventType\nfrom xmclaw.core.ir.toolcall import ToolSpec\nfrom xmclaw.daemon.history_utils import _is_transient_tool_error\nfrom xmclaw.daemon.turn_types import AgentTurnResult, _log_memory_failure\nfrom xmclaw.providers.llm.base import Message\nfrom xmclaw.security import SOURCE_TOOL_RESULT, apply_policy\nfrom xmclaw.utils.cost import BudgetExceeded\n\n\nclass HopLoopMixin:\n    """Provides the LLM ↔ tool hop loop."""'

content = content.replace(old_header, new_header)

with open('xmclaw/daemon/hop_loop.py', 'w', encoding='utf-8') as f:
    f.write(content)

print('Done')
