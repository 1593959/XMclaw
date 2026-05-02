"""Performance monitoring system for XMclaw.

Tracks:
- Response time per operation
- Token usage per model
- Tool execution time
- Error rates
- Memory usage
- LLM call latency
"""
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from xmclaw.utils.log import get_logger

logger = get_logger(__name__)


@dataclass
class OperationStats:
    """Statistics for a single operation type."""
    count: int = 0
    total_duration: float = 0.0
    min_duration: float = float('inf')
    max_duration: float = 0.0
    error_count: int = 0
    last_duration: float = 0.0
    last_timestamp: str = ""

    @property
    def avg_duration(self) -> float:
        return self.total_duration / self.count if self.count > 0 else 0.0

    @property
    def error_rate(self) -> float:
        return self.error_count / self.count if self.count > 0 else 0.0


@dataclass
class TokenUsage:
    """Token usage tracking."""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_estimate: float = 0.0


class PerformanceMonitor:
    """Central performance monitoring system.

    Tracks:
    - LLM response times by provider/model
    - Tool execution times by tool name
    - Error rates
    - Token usage
    - Agent loop cycles
    """

    def __init__(self):
        # Operation tracking by type
        self._llm_stats: dict[str, OperationStats] = defaultdict(OperationStats)
        self._tool_stats: dict[str, OperationStats] = defaultdict(OperationStats)
        self._agent_stats: dict[str, OperationStats] = defaultdict(OperationStats)

        # Token usage by provider
        self._token_usage: dict[str, TokenUsage] = defaultdict(TokenUsage)

        # Event tracking
        self._event_counts: dict[str, int] = defaultdict(int)

        # Conversation tracking
        self._active_conversations: dict[str, dict[str, Any]] = {}
        self._conversation_history: list[dict[str, Any]] = []

        # Timing context
        self._timing_stack: list[tuple[str, float]] = []

    # ── Timing Context Manager ────────────────────────────────────────────────

    def track_operation(self, operation_type: str):
        """Context manager for tracking operation duration.

        Usage:
            with monitor.track_operation("llm_call"):
                await some_llm_call()
        """
        return OperationTimer(self, operation_type)

    # ── LLM Tracking ──────────────────────────────────────────────────────────

    def track_llm_call(
        self,
        provider: str,
        model: str,
        duration: float,
        success: bool,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> None:
        """Record an LLM API call."""
        key = f"{provider}:{model}"
        stats = self._llm_stats[key]
        stats.count += 1
        stats.total_duration += duration
        stats.min_duration = min(stats.min_duration, duration)
        stats.max_duration = max(stats.max_duration, duration)
        stats.last_duration = duration
        stats.last_timestamp = datetime.now().isoformat()
        if not success:
            stats.error_count += 1

        # Token usage
        usage = self._token_usage[key]
        usage.prompt_tokens += prompt_tokens
        usage.completion_tokens += completion_tokens
        usage.total_tokens += prompt_tokens + completion_tokens

        # Rough cost estimate (varies by model)
        cost_per_1k = 0.01  # Approximate
        usage.cost_estimate += (prompt_tokens + completion_tokens) / 1000 * cost_per_1k

        logger.debug("llm_call_tracked",
                   provider=provider, model=model, duration=duration,
                   tokens=prompt_tokens + completion_tokens)

    def track_llm_error(self, provider: str, model: str) -> None:
        """Record an LLM error."""
        key = f"{provider}:{model}"
        stats = self._llm_stats[key]
        stats.error_count += 1

    # ── Tool Tracking ─────────────────────────────────────────────────────────

    def track_tool_call(
        self,
        tool_name: str,
        duration: float,
        success: bool,
    ) -> None:
        """Record a tool execution."""
        stats = self._tool_stats[tool_name]
        stats.count += 1
        stats.total_duration += duration
        stats.min_duration = min(stats.min_duration, duration)
        stats.max_duration = max(stats.max_duration, duration)
        stats.last_duration = duration
        stats.last_timestamp = datetime.now().isoformat()
        if not success:
            stats.error_count += 1

        logger.debug("tool_call_tracked",
                    tool=tool_name, duration=duration, success=success)

    # ── Agent Tracking ────────────────────────────────────────────────────────

    def track_agent_turn(
        self,
        agent_id: str,
        duration: float,
        tools_used: list[str],
        success: bool,
    ) -> None:
        """Record an agent turn."""
        stats = self._agent_stats[agent_id]
        stats.count += 1
        stats.total_duration += duration
        stats.min_duration = min(stats.min_duration, duration)
        stats.max_duration = max(stats.max_duration, duration)
        stats.last_duration = duration
        stats.last_timestamp = datetime.now().isoformat()
        if not success:
            stats.error_count += 1

        # Track tools used in this turn
        for tool in tools_used:
            self._event_counts[f"tool:{tool}"] += 1

    def track_conversation_start(self, conversation_id: str, agent_id: str) -> None:
        """Mark the start of a conversation."""
        self._active_conversations[conversation_id] = {
            "agent_id": agent_id,
            "start_time": time.time(),
            "turn_count": 0,
            "tool_calls": 0,
        }

    def track_conversation_end(
        self,
        conversation_id: str,
        success: bool,
        turns: int,
        total_duration: float,
    ) -> None:
        """Mark the end of a conversation."""
        conv = self._active_conversations.pop(conversation_id, {})
        record = {
            "conversation_id": conversation_id,
            "agent_id": conv.get("agent_id", "unknown"),
            "start_time": conv.get("start_time", time.time()),
            "end_time": time.time(),
            "duration": total_duration,
            "turns": turns,
            "success": success,
            "avg_turn_duration": total_duration / turns if turns > 0 else 0,
        }
        self._conversation_history.append(record)

        # Keep only last 1000 conversations
        if len(self._conversation_history) > 1000:
            self._conversation_history = self._conversation_history[-1000:]

    # ── Event Tracking ────────────────────────────────────────────────────────

    def track_event(self, event_type: str) -> None:
        """Track an event occurrence."""
        self._event_counts[event_type] += 1

    # ── Statistics Retrieval ─────────────────────────────────────────────────

    def get_llm_stats(self, provider_model: str = "") -> dict[str, Any]:
        """Get LLM statistics."""
        if provider_model:
            stats = self._llm_stats.get(provider_model)
            if stats:
                return self._stats_to_dict("llm", provider_model, stats)
            return {}

        return {
            key: self._stats_to_dict("llm", key, stats)
            for key, stats in self._llm_stats.items()
        }

    def get_tool_stats(self, tool_name: str = "") -> dict[str, Any]:
        """Get tool statistics."""
        if tool_name:
            stats = self._tool_stats.get(tool_name)
            if stats:
                return self._stats_to_dict("tool", tool_name, stats)
            return {}

        return {
            key: self._stats_to_dict("tool", key, stats)
            for key, stats in self._tool_stats.items()
        }

    def get_agent_stats(self, agent_id: str = "") -> dict[str, Any]:
        """Get agent statistics."""
        if agent_id:
            stats = self._agent_stats.get(agent_id)
            if stats:
                return self._stats_to_dict("agent", agent_id, stats)
            return {}

        return {
            key: self._stats_to_dict("agent", key, stats)
            for key, stats in self._agent_stats.items()
        }

    def get_token_usage(self) -> dict[str, Any]:
        """Get token usage by provider/model."""
        return {
            key: {
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
                "cost_estimate_usd": round(usage.cost_estimate, 4),
            }
            for key, usage in self._token_usage.items()
        }

    def get_event_summary(self) -> dict[str, int]:
        """Get event counts summary."""
        return dict(self._event_counts)

    def get_conversation_summary(self) -> dict[str, Any]:
        """Get conversation statistics summary."""
        if not self._conversation_history:
            return {
                "total_conversations": 0,
                "avg_duration": 0,
                "avg_turns": 0,
            }

        durations = [c["duration"] for c in self._conversation_history]
        turns = [c["turns"] for c in self._conversation_history]
        successes = sum(1 for c in self._conversation_history if c["success"])

        return {
            "total_conversations": len(self._conversation_history),
            "successful_conversations": successes,
            "success_rate": round(successes / len(self._conversation_history), 3),
            "avg_duration_seconds": round(sum(durations) / len(durations), 2),
            "avg_turns_per_conversation": round(sum(turns) / len(turns), 2),
            "total_turns": sum(turns),
        }

    def get_full_report(self) -> dict[str, Any]:
        """Get a complete performance report."""
        return {
            "timestamp": datetime.now().isoformat(),
            "llm": self.get_llm_stats(),
            "tools": self.get_tool_stats(),
            "agents": self.get_agent_stats(),
            "tokens": self.get_token_usage(),
            "events": self.get_event_summary(),
            "conversations": self.get_conversation_summary(),
        }

    def _stats_to_dict(self, category: str, name: str, stats: OperationStats) -> dict[str, Any]:
        return {
            "category": category,
            "name": name,
            "count": stats.count,
            "avg_duration_ms": round(stats.avg_duration * 1000, 2),
            "min_duration_ms": round(stats.min_duration * 1000, 2) if stats.min_duration != float('inf') else 0,
            "max_duration_ms": round(stats.max_duration * 1000, 2),
            "error_count": stats.error_count,
            "error_rate": round(stats.error_rate, 3),
            "last_duration_ms": round(stats.last_duration * 1000, 2),
            "last_timestamp": stats.last_timestamp,
        }

    def reset_stats(self) -> None:
        """Reset all statistics (useful for testing)."""
        self._llm_stats.clear()
        self._tool_stats.clear()
        self._agent_stats.clear()
        self._token_usage.clear()
        self._event_counts.clear()
        self._conversation_history.clear()


class OperationTimer:
    """Context manager for timing operations."""

    def __init__(self, monitor: PerformanceMonitor, operation_type: str):
        self.monitor = monitor
        self.operation_type = operation_type
        self.start_time: float = 0
        self.success: bool = True

    def __enter__(self):
        self.start_time = time.time()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        duration = time.time() - self.start_time
        self.success = exc_type is None

        if self.operation_type.startswith("llm:"):
            parts = self.operation_type.split(":", 1)
            if len(parts) == 2:
                provider_model = parts[1]
                if ":" in provider_model:
                    provider, model = provider_model.split(":", 1)
                else:
                    provider, model = "unknown", provider_model
                self.monitor.track_llm_call(provider, model, duration, self.success)
        elif self.operation_type.startswith("tool:"):
            tool_name = self.operation_type.split(":", 1)[1]
            self.monitor.track_tool_call(tool_name, duration, self.success)
        elif self.operation_type.startswith("agent:"):
            agent_id = self.operation_type.split(":", 1)[1]
            self.monitor.track_agent_turn(agent_id, duration, [], self.success)

        return False  # Don't suppress exceptions


# Global singleton
_monitor: PerformanceMonitor | None = None


def get_performance_monitor() -> PerformanceMonitor:
    """Get the global performance monitor instance."""
    global _monitor
    if _monitor is None:
        _monitor = PerformanceMonitor()
    return _monitor
