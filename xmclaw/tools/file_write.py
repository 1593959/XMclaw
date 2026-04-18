"""File write tool with permission and audit integration."""
import asyncio
from pathlib import Path
from xmclaw.tools.base import Tool
from xmclaw.utils.security import (
    is_path_safe, get_permission_manager, ToolCategory, AuditEntry,
)
from xmclaw.utils.paths import BASE_DIR


class FileWriteTool(Tool):
    name = "file_write"
    description = "Write content to a file. Creates parent directories if needed."
    parameters = {
        "file_path": {
            "type": "string",
            "description": "Absolute or workspace-relative path to the file.",
        },
        "content": {
            "type": "string",
            "description": "Content to write.",
        },
    }

    async def execute(self, file_path: str, content: str) -> str:
        target = Path(file_path)
        if not target.is_absolute():
            target = BASE_DIR / target

        path_str = str(target)

        if not is_path_safe(target, BASE_DIR):
            await self._log_blocked("Path outside workspace", path_str)
            return "[Blocked: Path is outside of allowed workspace]"

        # Check permission manager (handles rate limiting, ask/block, audit)
        pm = get_permission_manager()
        decision = pm.check_tool(self.name, context={"path": path_str})
        if not decision.allowed:
            if decision.requires_confirmation:
                confirmed = await pm.request_confirmation(self.name, decision.reason)
                if not confirmed:
                    return f"[Blocked: {decision.reason}]"
            else:
                return f"[Blocked: {decision.reason}]"

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"File written: {target}"

    async def _log_blocked(self, reason: str, path: str) -> None:
        try:
            from datetime import datetime
            pm = get_permission_manager()
            asyncio.create_task(pm._audit_log_entry(AuditEntry(
                timestamp=datetime.now().isoformat(),
                event="BLOCKED",
                tool=self.name,
                user="default",
                detail=f"{reason}: {path}",
                tool_category=ToolCategory.MODERATE.value,
                risk_score=40,
            )))
        except Exception:
            pass
