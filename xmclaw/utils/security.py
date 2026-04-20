"""Unified security permission system for XMclaw.

Provides:
- Permission levels: allow / ask / block per tool
- Tool categories: safe / moderate / dangerous
- File path sandboxing
- Network access control
- Security audit logging
"""
from __future__ import annotations

import asyncio
import functools
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from xmclaw.utils.paths import BASE_DIR

# ── Enums ───────────────────────────────────────────────────────────────────────

class PermissionLevel(Enum):
    """Permission levels for tool execution."""
    ALLOW = "allow"      # Always permitted
    ASK   = "ask"        # Requires user confirmation
    BLOCK = "block"      # Always denied

class ToolCategory(Enum):
    """Risk categories for grouping tools."""
    SAFE      = "safe"       # Read-only, no side effects
    MODERATE  = "moderate"   # Some side effects, contained
    DANGEROUS = "dangerous" # High risk, unrestricted access


# ── Default tool registry ───────────────────────────────────────────────────────

TOOL_CATEGORIES: dict[str, ToolCategory] = {
    # Safe - read-only
    "file_read":     ToolCategory.SAFE,
    "glob":          ToolCategory.SAFE,
    "grep":          ToolCategory.SAFE,
    "web_search":    ToolCategory.SAFE,
    "web_fetch":     ToolCategory.SAFE,
    "memory_search": ToolCategory.SAFE,
    "git":           ToolCategory.SAFE,       # read-only ops only
    "ask_user":      ToolCategory.SAFE,
    "todo":          ToolCategory.SAFE,
    "vision":       ToolCategory.SAFE,
    "asr":           ToolCategory.SAFE,
    "tts":           ToolCategory.SAFE,
    "code_exec":     ToolCategory.SAFE,       # ephemeral in-process exec

    # Moderate - contained side effects
    "file_write":    ToolCategory.MODERATE,
    "file_edit":     ToolCategory.MODERATE,
    "bash":          ToolCategory.MODERATE,   # restricted by pattern checks
    "task_tool":     ToolCategory.MODERATE,
    "agent_tool":    ToolCategory.MODERATE,
    "skill":         ToolCategory.MODERATE,
    "mcp":           ToolCategory.MODERATE,

    # Dangerous - full system access
    "computer_use":  ToolCategory.DANGEROUS,
    "browser":       ToolCategory.DANGEROUS,
    "github":        ToolCategory.DANGEROUS,  # external write access
}

DEFAULT_PERMISSIONS: dict[ToolCategory, PermissionLevel] = {
    ToolCategory.SAFE:      PermissionLevel.ALLOW,
    ToolCategory.MODERATE:  PermissionLevel.ALLOW,  # ASK requires user confirmation; ALLOW for interactive use
    ToolCategory.DANGEROUS: PermissionLevel.BLOCK,
}


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class SecurityDecision:
    """Result of a permission check."""
    allowed: bool
    level: PermissionLevel
    reason: str
    requires_confirmation: bool = False


@dataclass
class AuditEntry:
    """A single security audit log entry."""
    timestamp: str
    event: str          # BLOCKED, ALLOWED, ASKED, RATE_LIMITED
    tool: str
    user: str
    detail: str
    tool_category: str
    risk_score: int     # 0-100

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "event": self.event,
            "tool": self.tool,
            "user": self.user,
            "detail": self.detail,
            "category": self.tool_category,
            "risk_score": self.risk_score,
        }


# ── Permission Manager ─────────────────────────────────────────────────────────

class PermissionManager:
    """Unified security permission system.

    Usage:
        pm = PermissionManager()
        decision = pm.check_tool("bash", user="alice", context={...})
        if decision.requires_confirmation:
            # ask user, then call pm.confirm_tool(...)
        elif not decision.allowed:
            return f"[Blocked: {decision.reason}]"
        # proceed with tool execution
    """

    def __init__(self, config: dict | None = None):
        self.config = config or {}

        # Permission overrides per tool: tool_name -> PermissionLevel
        self._tool_overrides: dict[str, PermissionLevel] = {}
        self._load_config(config)

        # Pending confirmations: tool_name -> asyncio.Event
        self._pending: dict[str, asyncio.Event] = {}
        self._pending_results: dict[str, bool] = {}

        # Audit log (in-memory, written to file periodically)
        self._audit_log: list[AuditEntry] = []
        self._audit_file = BASE_DIR / "shared" / "security_audit.log"
        self._audit_lock = asyncio.Lock()

        # Network access control
        self._network_allowed_patterns: list[re.Pattern] = []
        self._load_network_patterns()

    def _load_config(self, config: dict | None) -> None:
        """Load permissions from config dict."""
        if not config:
            return
        sec = config.get("security", {})
        # Load tool overrides
        for tool, level_str in sec.get("tool_permissions", {}).items():
            try:
                self._tool_overrides[tool] = PermissionLevel(level_str)
            except ValueError:
                pass

    def _load_network_patterns(self) -> None:
        """Compile allowed network destination patterns."""
        patterns = [
            r"^https?://",                    # Any HTTP/HTTPS
            r"^ws[s]?://",                   # WebSocket
            r"^[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",  # Hostname pattern
        ]
        for p in self.config.get("security", {}).get("network_allowed_patterns", []):
            try:
                self._compile_pattern(p)
            except re.error:
                self._compile_pattern(patterns[0])  # fallback to allow all

    def _compile_pattern(self, p: str) -> re.Pattern:
        compiled = re.compile(p, re.IGNORECASE)
        self._network_allowed_patterns.append(compiled)
        return compiled

    # ── Tool permission checks ────────────────────────────────────────────────

    def check_tool(self, tool_name: str, user: str = "default",
                   context: dict | None = None) -> SecurityDecision:
        """Check if a tool is permitted to run.

        Args:
            tool_name: Tool identifier
            user: User making the request
            context: Execution context (command, path, etc.)
        """
        context = context or {}
        category = TOOL_CATEGORIES.get(tool_name, ToolCategory.MODERATE)

        # 1. Check explicit override
        level = self._tool_overrides.get(tool_name)
        if level is None:
            level = DEFAULT_PERMISSIONS.get(category, PermissionLevel.ASK)

        # 2. Check file path sandbox (for file/path operations)
        if "path" in context:
            if not self._check_path_sandbox(context["path"]):
                return self._make_decision(
                    allowed=False, level=PermissionLevel.BLOCK,
                    reason="Path is outside the allowed workspace",
                    user=user, tool=tool_name, category=category,
                    detail=f"Blocked path: {context['path']}"
                )

        # 3. Check network access (for network tools)
        if "url" in context:
            if not self._check_network_access(context["url"]):
                return self._make_decision(
                    allowed=False, level=PermissionLevel.BLOCK,
                    reason="Network destination not allowed",
                    user=user, tool=tool_name, category=category,
                    detail=f"Blocked URL: {context['url']}"
                )

        # 4. Determine decision
        if level == PermissionLevel.BLOCK:
            return self._make_decision(
                allowed=False, level=level,
                reason=f"{tool_name} is explicitly blocked by security policy",
                user=user, tool=tool_name, category=category,
                detail=f"Tool blocked: {tool_name}"
            )

        if level == PermissionLevel.ASK:
            return self._make_decision(
                allowed=False, level=level,
                reason=f"{tool_name} requires user confirmation",
                user=user, tool=tool_name, category=category,
                detail=f"Tool requires confirmation: {tool_name}",
                requires_confirmation=True
            )

        # ALLOW
        return self._make_decision(
            allowed=True, level=level,
            reason=f"{tool_name} is permitted",
            user=user, tool=tool_name, category=category,
            detail=f"Tool allowed: {tool_name}"
        )

    def _check_path_sandbox(self, path_str: str) -> bool:
        """Check if a path is within the allowed workspace."""
        try:
            p = Path(path_str).resolve()
            p.relative_to(BASE_DIR.resolve())
            return True
        except (ValueError, OSError):
            return False

    def _check_network_access(self, url: str) -> bool:
        """Check if a URL matches allowed network patterns."""
        if not self._network_allowed_patterns:
            return True  # No restrictions
        for pattern in self._network_allowed_patterns:
            if pattern.search(url):
                return True
        return False

    def _make_decision(self, allowed: bool, level: PermissionLevel,
                       reason: str, user: str, tool: str,
                       category: ToolCategory, detail: str,
                       requires_confirmation: bool = False) -> SecurityDecision:
        """Create a decision and log it."""
        event = "ALLOWED" if allowed else ("ASKED" if requires_confirmation else "BLOCKED")
        risk = self._compute_risk_score(tool, category, allowed)

        entry = AuditEntry(
            timestamp=datetime.now().isoformat(),
            event=event,
            tool=tool,
            user=user,
            detail=detail,
            tool_category=category.value,
            risk_score=risk,
        )

        # Log safely from any context (sync or async)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._audit_log_entry(entry))
        except RuntimeError:
            # No event loop — log synchronously
            self._audit_log.append(entry)
            if len(self._audit_log) > 10000:
                self._audit_log = self._audit_log[-10000:]
            try:
                self._audit_file.parent.mkdir(parents=True, exist_ok=True)
                with open(self._audit_file, "a", encoding="utf-8") as f:
                    f.write(entry.timestamp + "\t" + "\t".join([
                        entry.event, entry.tool, entry.user,
                        entry.detail, entry.tool_category, str(entry.risk_score)
                    ]) + "\n")
            except Exception:
                pass

        return SecurityDecision(
            allowed=allowed,
            level=level,
            reason=reason,
            requires_confirmation=requires_confirmation,
        )

    def _compute_risk_score(self, tool: str, category: ToolCategory,
                            allowed: bool) -> int:
        """Compute a 0-100 risk score for audit logging."""
        base = {"safe": 10, "moderate": 40, "dangerous": 80}.get(category.value, 30)
        if not allowed:
            return base  # Risk was detected but blocked
        return base

    async def _audit_log_entry(self, entry: AuditEntry) -> None:
        """Append entry to audit log and persist to file."""
        self._audit_log.append(entry)
        # Keep last 10000 entries in memory
        if len(self._audit_log) > 10000:
            self._audit_log = self._audit_log[-10000:]

        async with self._audit_lock:
            self._audit_file.parent.mkdir(parents=True, exist_ok=True)
            try:
                with open(self._audit_file, "a", encoding="utf-8") as f:
                    f.write(entry.timestamp + "\t" + "\t".join([
                        entry.event, entry.tool, entry.user,
                        entry.detail, entry.tool_category, str(entry.risk_score)
                    ]) + "\n")
            except Exception:
                pass  # Don't let audit log failures break tool execution

    # ── Confirmation flow ─────────────────────────────────────────────────────

    async def request_confirmation(self, tool_name: str, reason: str,
                                   timeout: float = 120.0) -> bool:
        """Wait for user to confirm or deny a tool execution.

        Returns True if confirmed, False if denied or timed out.
        """
        event = asyncio.Event()
        self._pending[tool_name] = event
        self._pending_results[tool_name] = False

        try:
            try:
                await asyncio.wait_for(event.wait(), timeout=timeout)
                return self._pending_results.get(tool_name, False)
            except asyncio.TimeoutError:
                return False
        finally:
            self._pending.pop(tool_name, None)
            self._pending_results.pop(tool_name, None)

    def confirm_tool(self, tool_name: str, approved: bool) -> None:
        """Called by UI/user to approve or deny a pending tool."""
        if tool_name in self._pending:
            self._pending_results[tool_name] = approved
            self._pending[tool_name].set()

    # ── Permission management ─────────────────────────────────────────────────

    def set_tool_permission(self, tool_name: str, level: PermissionLevel) -> None:
        """Change permission for a specific tool at runtime."""
        self._tool_overrides[tool_name] = level

    def get_tool_permission(self, tool_name: str) -> PermissionLevel:
        """Get current permission level for a tool."""
        return self._tool_overrides.get(
            tool_name,
            DEFAULT_PERMISSIONS.get(
                TOOL_CATEGORIES.get(tool_name, ToolCategory.MODERATE),
                PermissionLevel.ASK
            )
        )

    def list_permissions(self) -> dict[str, dict]:
        """Return current permission state for all known tools."""
        result = {}
        for tool in TOOL_CATEGORIES:
            level = self.get_tool_permission(tool)
            result[tool] = {
                "category": TOOL_CATEGORIES[tool].value,
                "permission": level.value,
                "is_override": tool in self._tool_overrides,
            }
        return result

    # ── Audit log access ─────────────────────────────────────────────────────

    def get_audit_log(self, limit: int = 100,
                       event_filter: str | None = None) -> list[AuditEntry]:
        """Retrieve recent audit log entries."""
        entries = self._audit_log[-limit:]
        if event_filter:
            entries = [e for e in entries if e.event == event_filter]
        return entries

    def get_audit_summary(self) -> dict:
        """Get a summary of security events."""
        events: dict[str, int] = {}
        blocked_paths = 0
        total_risk = 0
        for e in self._audit_log:
            events[e.event] = events.get(e.event, 0) + 1
            if e.event == "BLOCKED" and "path" in e.detail.lower():
                blocked_paths += 1
            total_risk += e.risk_score

        count = len(self._audit_log) or 1
        return {
            "total_events": count,
            "by_event": events,
            "blocked_path_attempts": blocked_paths,
            "average_risk_score": round(total_risk / count, 1),
            "top_blocked_tools": self._top_blocked_tools(),
        }

    def _top_blocked_tools(self) -> list[dict]:
        counts: dict[str, int] = {}
        for e in self._audit_log:
            if e.event == "BLOCKED":
                counts[e.tool] = counts.get(e.tool, 0) + 1
        return sorted(counts.items(), key=lambda x: -x[1])[:5]

    def export_permissions_config(self) -> dict:
        """Export current permission config for saving to config.json."""
        return {
            "tool_permissions": {
                tool: level.value
                for tool, level in self._tool_overrides.items()
            }
        }


# ── Decorator for tool permission checks ──────────────────────────────────────

def require_permission(level: PermissionLevel = PermissionLevel.ALLOW):
    """Decorator: enforce permission check before tool execution.

    Usage:
        class MyTool(Tool):
            @require_permission(PermissionLevel.MODERATE)
            async def execute(self, ...):
                ...
    """
    def decorator(func):
        @functools.wraps(func)
        def sync_wrapper(self, *args, **kwargs):
            # For sync wrappers, we can't do async here — handled in execute()
            return func(self, *args, **kwargs)

        @functools.wraps(func)
        async def async_wrapper(self, *args, **kwargs):
            # Get tool name from self
            tool_name = getattr(self, "name", func.__name__)
            pm = get_permission_manager()
            decision = pm.check_tool(tool_name)

            if not decision.allowed:
                if decision.requires_confirmation:
                    confirmed = await pm.request_confirmation(
                        tool_name, decision.reason
                    )
                    if not confirmed:
                        return f"[Blocked: {decision.reason}]"
                else:
                    return f"[Blocked: {decision.reason}]"

            return await func(self, *args, **kwargs)

        # Return async wrapper if the function is async
        import inspect
        if inspect.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper
    return decorator


# ── Singleton accessor ─────────────────────────────────────────────────────────

_permission_manager: PermissionManager | None = None


def get_permission_manager() -> PermissionManager:
    """Return the global PermissionManager instance."""
    global _permission_manager
    if _permission_manager is None:
        _permission_manager = PermissionManager()
    return _permission_manager


def set_permission_manager(pm: PermissionManager) -> None:
    """Set the global PermissionManager instance."""
    global _permission_manager
    _permission_manager = pm


# ── Legacy path check (kept for backward compatibility) ───────────────────────

def is_path_safe(target: Path, base: Path) -> bool:
    """Ensure target is within base directory (legacy, use PermissionManager)."""
    try:
        target.resolve().relative_to(base.resolve())
        return True
    except (ValueError, OSError):
        return False
