"""XMclaw — local-first, self-evolving AI agent runtime.

Package layout (v2; v1 strangler-fig sweep complete):

    xmclaw/core/{bus,grader,scheduler,ir,session,evolution}/
    xmclaw/providers/{llm,memory,channel,tool,runtime}/
    xmclaw/daemon/       FastAPI + WS + AgentLoop + lifecycle
    xmclaw/security/     prompt-injection scanner + policy gate
    xmclaw/skills/       SkillBase + registry + demo skills
    xmclaw/cli/          ``xmclaw`` entry point + doctor
    xmclaw/utils/        paths, log, redact, cost
    xmclaw/plugins/      third-party plugin loader (Epic #2 WIP)

Per-subdir contracts live in ``<subdir>/AGENTS.md``.
"""

__version__ = "2.0.0.dev0"
