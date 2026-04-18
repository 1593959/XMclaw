"""File edit tool (find and replace) with permission and audit integration."""
import asyncio
from datetime import datetime
from pathlib import Path
from xmclaw.tools.base import Tool
from xmclaw.utils.security import (
    is_path_safe, get_permission_manager, ToolCategory, AuditEntry,
)
from xmclaw.utils.paths import BASE_DIR


class FileEditTool(Tool):
    name = "file_edit"
    description = "Find and replace text in a file."
    parameters = {
        "file_path": {
            "type": "string",
            "description": "Path to the file.",
        },
        "old_text": {
            "type": "string",
            "description": "Exact text to find.",
        },
        "new_text": {
            "type": "string",
            "description": "Text to replace with.",
        },
    }

    async def execute(self, file_path: str, old_text: str, new_text: str) -> str:
        target = Path(file_path)
        if not target.is_absolute():
            target = BASE_DIR / target

        path_str = str(target)

        if not is_path_safe(target, BASE_DIR):
            await self._log_security_event("BLOCKED", f"Path outside workspace: {path_str}")
            return "[Blocked: Path is outside of allowed workspace]"

        # Check permission manager
        pm = get_permission_manager()
        decision = pm.check_tool(self.name, context={"path": path_str})
        if not decision.allowed:
            if decision.requires_confirmation:
                confirmed = await pm.request_confirmation(self.name, decision.reason)
                if not confirmed:
                    return f"[Blocked: {decision.reason}]"
            else:
                return f"[Blocked: {decision.reason}]"

        if not target.exists():
            return f"[Error: File not found: {file_path}]"

        content = target.read_text(encoding="utf-8")
        if old_text not in content:
            return "[Error: old_text not found in file]"

        content = content.replace(old_text, new_text)
        target.write_text(content, encoding="utf-8")
        return f"File edited: {target}"

    async def _log_security_event(self, event: str, detail: str) -> None:
        try:
            pm = get_permission_manager()
            asyncio.create_task(pm._audit_log_entry(AuditEntry(
                timestamp=datetime.now().isoformat(),
                event=event,
                tool=self.name,
                user="default",
                detail=detail,
                tool_category=ToolCategory.MODERATE.value,
                risk_score=40 if event == "BLOCKED" else 20,
            )))
        except Exception:
            pass
