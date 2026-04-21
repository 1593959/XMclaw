"""XMclaw — local-first, self-evolving AI agent runtime.

This is the v2-rewrite branch. v1 modules still live alongside v2 during the
transition (see docs/REWRITE_PLAN.md §7 salvage map). New code goes under:

    xmclaw/core/{bus,grader,scheduler,ir,session}/
    xmclaw/providers/{llm,memory,channel,tool,runtime}/
    xmclaw/skills/
    xmclaw/plugins/

v1 modules scheduled for removal are tagged with ``# V1-LEGACY`` at top.
"""

__version__ = "2.0.0.dev0"
