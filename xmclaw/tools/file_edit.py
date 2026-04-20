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
    description = (
        "Edit a file.  mode='replace' (default) does exact find-and-replace; "
        "mode='append' appends text to the end (creates the file if missing). "
        "Use append for append-only logs like workspace/decisions.md."
    )
    parameters = {
        "file_path": {
            "type": "string",
            "description": "Path to the file.",
        },
        "old_text": {
            "type": "string",
            "description": "Exact text to find.  Required for replace mode, ignored for append.",
        },
        "new_text": {
            "type": "string",
            "description": "Text to insert.  In replace mode replaces old_text; in append mode is written at end of file.",
        },
        "mode": {
            "type": "string",
            "description": "'replace' (default) or 'append'.",
        },
    }

    async def execute(
        self,
        file_path: str,
        new_text: str,
        old_text: str | None = None,
        mode: str = "replace",
    ) -> str:
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

        if mode == "append":
            # Append creates the file if missing — callers shouldn't have
            # to initialize decisions.md / notes.md before the first entry.
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "a", encoding="utf-8") as f:
                f.write(new_text)
            return f"File appended: {target}"

        if mode != "replace":
            return f"[Error: Unknown mode '{mode}'.  Use 'replace' or 'append'.]"

        if old_text is None:
            return "[Error: old_text is required for replace mode]"

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
