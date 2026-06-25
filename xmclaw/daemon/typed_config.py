"""Typed overlays for high-risk daemon configuration blocks."""
from __future__ import annotations

from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    ValidationError,
)


class ShellConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    execution_policy: Literal["host_guarded", "docker", "disabled"] = "host_guarded"
    sandbox_image: str = Field(default="python:3.12-alpine", min_length=1)
    image: str | None = None
    sandbox_memory: str = Field(default="512m", min_length=1)
    memory: str | None = None
    sandbox_cpus: str = Field(default="1.0", min_length=1)
    cpus: str | None = None
    sandbox_pids_limit: StrictInt = Field(default=256, ge=16, le=4096)
    pids_limit: StrictInt | None = Field(default=None, ge=16, le=4096)
    sandbox_network: Literal["none", "bridge"] = "none"
    network: Literal["none", "bridge"] | None = None

    @property
    def resolved_image(self) -> str:
        if "sandbox_image" in self.model_fields_set:
            return self.sandbox_image
        return self.image or self.sandbox_image or "python:3.12-alpine"

    @property
    def resolved_memory(self) -> str:
        if "sandbox_memory" in self.model_fields_set:
            return self.sandbox_memory
        return self.memory or self.sandbox_memory or "512m"

    @property
    def resolved_cpus(self) -> str:
        if "sandbox_cpus" in self.model_fields_set:
            return self.sandbox_cpus
        return self.cpus or self.sandbox_cpus or "1.0"

    @property
    def resolved_pids_limit(self) -> int:
        if "sandbox_pids_limit" in self.model_fields_set:
            return self.sandbox_pids_limit
        return self.pids_limit or self.sandbox_pids_limit

    @property
    def resolved_network(self) -> str:
        if "sandbox_network" in self.model_fields_set:
            return self.sandbox_network
        return self.network or self.sandbox_network


class ToolsConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enable_bash: StrictBool = True
    enable_web: StrictBool = True
    invoke_timeout_s: StrictFloat | StrictInt | None = Field(
        default=None,
        ge=1,
        le=600,
    )
    shell_execution_policy: Literal["host_guarded", "docker", "disabled"] | None = None
    shell: ShellConfig = Field(default_factory=ShellConfig)

    @property
    def resolved_shell_policy(self) -> str:
        if "execution_policy" in self.shell.model_fields_set:
            return self.shell.execution_policy
        return self.shell_execution_policy or self.shell.execution_policy or "host_guarded"


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    max_hops: StrictInt | None = Field(default=None, ge=1, le=100)
    max_react_loop: StrictInt | None = Field(default=None, ge=1, le=100)

    @property
    def resolved_max_hops(self) -> int:
        return self.max_react_loop or self.max_hops or 100


class SwarmConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: StrictBool | None = None
    max_subagents: StrictInt | None = Field(default=None, ge=2, le=64)
    max_depth: StrictInt | None = Field(default=None, ge=1, le=8)
    task_timeout_s: StrictFloat | StrictInt | None = Field(default=None, gt=0)
    synthesize: StrictBool | None = None


class SkillProposerConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    history_window: StrictInt | None = Field(default=None, ge=1, le=1000)
    min_pattern_count: StrictInt | None = Field(default=None, ge=1, le=100)
    min_confidence: StrictFloat | StrictInt | None = Field(default=None, ge=0, le=1)


class EvolutionSkillDreamConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: StrictBool | None = None
    interval_s: StrictFloat | StrictInt | None = Field(default=None, gt=0)


class EvolutionRealtimeConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: StrictBool | None = None
    debounce_s: StrictFloat | StrictInt | None = Field(default=None, gt=0)
    cooldown_s: StrictFloat | StrictInt | None = Field(default=None, ge=0)


class EvolutionConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    skill_dream: EvolutionSkillDreamConfig = Field(
        default_factory=EvolutionSkillDreamConfig,
    )
    realtime: EvolutionRealtimeConfig = Field(
        default_factory=EvolutionRealtimeConfig,
    )


class SelfCritiqueConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: StrictBool = True


class ContinuousLoopConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    autonomy_level: StrictInt | None = Field(default=None, ge=0, le=100)
    heartbeat_hz: StrictFloat | StrictInt | None = Field(default=None, gt=0)


class AutoRecallConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: StrictBool | None = None
    use_hybrid: StrictBool | None = None
    timeout_s: StrictFloat | StrictInt | None = Field(default=None, gt=0)
    min_similarity: StrictFloat | StrictInt | None = Field(
        default=None,
        ge=0,
        le=1,
    )


class MemoryRetentionConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sweep_interval_s: StrictFloat | StrictInt | None = Field(default=None, ge=0)
    dedup_every_n_sweeps: StrictInt | None = Field(default=None, ge=0)
    llm_dedup_every_n_sweeps: StrictInt | None = Field(default=None, ge=0)
    dedup_scopes: list[StrictStr] | None = None


class MemoryCuratorConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: StrictBool | None = None
    announce: StrictBool | None = None
    do_dedup: StrictBool | None = None
    do_prune: StrictBool | None = None
    do_contradict: StrictBool | None = None
    do_crystallize: StrictBool | None = None
    interval_s: StrictFloat | StrictInt | None = Field(default=None, gt=0)
    check_interval_s: StrictFloat | StrictInt | None = Field(default=None, gt=0)
    warmup_s: StrictFloat | StrictInt | None = Field(default=None, gt=0)
    time_budget_s: StrictFloat | StrictInt | None = Field(default=None, gt=0)
    scopes: list[StrictStr] | None = None


class MemoryWriteDecisionConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: StrictBool | None = None


class MemoryV2Config(BaseModel):
    model_config = ConfigDict(extra="ignore")

    retention: MemoryRetentionConfig = Field(default_factory=MemoryRetentionConfig)
    curator: MemoryCuratorConfig = Field(default_factory=MemoryCuratorConfig)
    write_decision: MemoryWriteDecisionConfig = Field(
        default_factory=MemoryWriteDecisionConfig,
    )


class CognitionConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    continuous_loop: ContinuousLoopConfig = Field(
        default_factory=ContinuousLoopConfig,
    )
    auto_recall: AutoRecallConfig = Field(default_factory=AutoRecallConfig)
    self_critique: SelfCritiqueConfig = Field(default_factory=SelfCritiqueConfig)
    skill_proposer: SkillProposerConfig = Field(default_factory=SkillProposerConfig)
    memory_v2: MemoryV2Config = Field(default_factory=MemoryV2Config)


class DaemonTypedConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    swarm: SwarmConfig = Field(default_factory=SwarmConfig)
    evolution: EvolutionConfig = Field(default_factory=EvolutionConfig)
    cognition: CognitionConfig = Field(default_factory=CognitionConfig)


def parse_typed_config(cfg: dict) -> DaemonTypedConfig:
    return DaemonTypedConfig.model_validate(cfg)


def typed_config_errors(cfg: dict) -> list[str]:
    try:
        parse_typed_config(cfg)
    except ValidationError as exc:
        return [
            ".".join(str(part) for part in err["loc"]) + f": {err['msg']}"
            for err in exc.errors()
        ]
    return []


__all__ = [
    "AgentConfig",
    "AutoRecallConfig",
    "ContinuousLoopConfig",
    "CognitionConfig",
    "DaemonTypedConfig",
    "EvolutionConfig",
    "EvolutionRealtimeConfig",
    "EvolutionSkillDreamConfig",
    "MemoryCuratorConfig",
    "MemoryRetentionConfig",
    "MemoryV2Config",
    "MemoryWriteDecisionConfig",
    "SelfCritiqueConfig",
    "ShellConfig",
    "SkillProposerConfig",
    "SwarmConfig",
    "ToolsConfig",
    "parse_typed_config",
    "typed_config_errors",
]
