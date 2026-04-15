"""Session manager using JSONL files."""
import json
from pathlib import Path
from typing import Any


class SessionManager:
    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _get_file(self, agent_id: str) -> Path:
        return self.base_dir / f"{agent_id}.jsonl"

    async def append(self, agent_id: str, record: dict[str, Any]) -> None:
        file_path = self._get_file(agent_id)
        with open(file_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        # Event-driven progress tracking
        self._trigger_progress_tracking()

    async def get_recent(self, agent_id: str, limit: int = 10) -> list[dict[str, Any]]:
        file_path = self._get_file(agent_id)
        if not file_path.exists():
            return []
        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        records = [json.loads(line) for line in lines if line.strip()]
        return records[-limit:]

    async def get_all(self, agent_id: str) -> list[dict[str, Any]]:
        file_path = self._get_file(agent_id)
        if not file_path.exists():
            return []
        with open(file_path, "r", encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def _trigger_progress_tracking(self) -> None:
        """Trigger progress tracker on every conversation turn."""
        import subprocess
        import sys
        tracker = Path(__file__).parent.parent.parent / ".tracker" / "progress_tracker.py"
        if tracker.exists():
            try:
                subprocess.Popen(
                    [sys.executable, str(tracker)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass
