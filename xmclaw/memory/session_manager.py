"""Session manager using JSONL files with import/export support."""
import json
import shutil
import zipfile
from datetime import datetime
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

    # ── Import/Export ──────────────────────────────────────────────────────────

    def export_session(
        self,
        agent_id: str,
        output_path: Path | str | None = None,
        format: str = "jsonl",
    ) -> Path:
        """Export a session to a file.

        Args:
            agent_id: Agent ID to export
            output_path: Output file path (auto-generated if None)
            format: Export format - 'jsonl', 'json', or 'zip'

        Returns:
            Path to the base output file (without format suffix appended;
            use e.g. ``path.with_suffix('.jsonl')`` to get the actual file path).
        """
        records = self._read_session_sync(agent_id)
        if not records:
            raise ValueError(f"No session found for agent: {agent_id}")

        if output_path is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = self.base_dir / f"export_{agent_id}_{timestamp}"

        output_path = Path(output_path)

        if format == "jsonl":
            return self._export_jsonl(records, output_path)
        elif format == "json":
            return self._export_json(records, output_path)
        elif format == "zip":
            return self._export_zip(records, output_path)
        else:
            raise ValueError(f"Unknown export format: {format}")

    def _read_session_sync(self, agent_id: str) -> list[dict]:
        """Synchronously read session records."""
        file_path = self._get_file(agent_id)
        if not file_path.exists():
            return []
        with open(file_path, "r", encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def _export_jsonl(self, records: list[dict], output_path: Path) -> Path:
        """Export as JSONL (one JSON object per line)."""
        output_path = Path(output_path).with_suffix(".jsonl")
        with open(output_path, "w", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(f"Exported {len(records)} records to {output_path}")
        return output_path

    def _export_json(self, records: list[dict], output_path: Path) -> Path:
        """Export as a single JSON array."""
        output_path = Path(output_path).with_suffix(".json")
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump({
                "agent_id": records[0].get("agent_id", "unknown") if records else "unknown",
                "exported_at": datetime.now().isoformat(),
                "record_count": len(records),
                "records": records,
            }, f, ensure_ascii=False, indent=2)
        print(f"Exported {len(records)} records to {output_path}")
        return output_path

    def _export_zip(self, records: list[dict], output_path: Path) -> Path:
        """Export as a ZIP file containing JSONL and metadata."""
        output_path = Path(output_path)
        output_path = output_path.with_suffix(".zip")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
            # Add records as JSONL
            jsonl_content = "\n".join(json.dumps(r, ensure_ascii=False) for r in records)
            zf.writestr(f"sessions/{timestamp}.jsonl", jsonl_content)

            # Add metadata
            metadata = {
                "agent_id": records[0].get("agent_id", "unknown") if records else "unknown",
                "exported_at": datetime.now().isoformat(),
                "record_count": len(records),
                "export_version": "1.0",
            }
            zf.writestr("metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2))

        print(f"Exported {len(records)} records to {output_path}")
        return output_path

    def import_session(
        self,
        input_path: Path | str,
        agent_id: str | None = None,
        mode: str = "replace",
    ) -> int:
        """Import a session from a file.

        Args:
            input_path: Path to import from (JSONL, JSON, or ZIP)
            agent_id: Target agent ID (extracted from filename if None)
            mode: Import mode - 'replace' (overwrite), 'append', or 'merge'

        Returns:
            Number of records imported
        """
        input_path = Path(input_path)
        if not input_path.exists():
            raise FileNotFoundError(f"Import file not found: {input_path}")

        records = self._import_read(input_path)
        if not records:
            return 0

        # Determine target agent_id
        if agent_id is None:
            agent_id = input_path.stem.replace("export_", "").split("_")[0]

        if mode == "replace":
            return self._import_replace(agent_id, records)
        elif mode == "append":
            return self._import_append(agent_id, records)
        elif mode == "merge":
            return self._import_merge(agent_id, records)
        else:
            raise ValueError(f"Unknown import mode: {mode}")

    def _import_read(self, input_path: Path) -> list[dict]:
        """Read records from various file formats."""
        input_path = Path(input_path)

        if input_path.suffix == ".zip":
            return self._import_from_zip(input_path)
        elif input_path.suffix == ".json":
            return self._import_from_json(input_path)
        else:  # .jsonl or other
            return self._import_from_jsonl(input_path)

    def _import_from_jsonl(self, input_path: Path) -> list[dict]:
        """Read records from JSONL file."""
        with open(input_path, "r", encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def _import_from_json(self, input_path: Path) -> list[dict]:
        """Read records from JSON array file."""
        with open(input_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
            elif isinstance(data, dict) and "records" in data:
                return data["records"]
        return []

    def _import_from_zip(self, input_path: Path) -> list[dict]:
        """Read records from ZIP file."""
        records = []
        with zipfile.ZipFile(input_path, "r") as zf:
            # Find and read JSONL files
            for name in zf.namelist():
                if name.endswith(".jsonl"):
                    content = zf.read(name).decode("utf-8")
                    records.extend(json.loads(line) for line in content.splitlines() if line.strip())
        return records

    def _import_replace(self, agent_id: str, records: list[dict]) -> int:
        """Replace existing session with imported records."""
        file_path = self._get_file(agent_id)
        # Backup existing
        if file_path.exists():
            backup_path = file_path.with_suffix(".jsonl.bak")
            shutil.copy2(file_path, backup_path)

        # Write new records
        with open(file_path, "w", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

        print(f"Replaced {len(records)} records for agent: {agent_id}")
        return len(records)

    def _import_append(self, agent_id: str, records: list[dict]) -> int:
        """Append imported records to existing session."""
        file_path = self._get_file(agent_id)
        count = 0
        with open(file_path, "a", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                count += 1

        print(f"Appended {count} records for agent: {agent_id}")
        return count

    def _import_merge(self, agent_id: str, records: list[dict]) -> int:
        """Merge imported records with existing, removing duplicates."""
        existing = self._read_session_sync(agent_id)

        # Create fingerprint for deduplication
        def fingerprint(r: dict) -> str:
            ts = r.get("timestamp", "")
            user = r.get("user", "")[:50]
            return f"{ts}:{user}"

        existing_fps = {fingerprint(r) for r in existing}
        new_records = [r for r in records if fingerprint(r) not in existing_fps]

        if new_records:
            file_path = self._get_file(agent_id)
            with open(file_path, "a", encoding="utf-8") as f:
                for record in new_records:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")

        print(f"Merged {len(new_records)} new records (skipped {len(records) - len(new_records)} duplicates)")
        return len(new_records)

    def list_exports(self) -> list[dict]:
        """List available export files."""
        exports = []
        for f in self.base_dir.glob("export_*"):
            if f.suffix in (".jsonl", ".json", ".zip"):
                stat = f.stat()
                exports.append({
                    "file": f.name,
                    "path": str(f),
                    "format": f.suffix.lstrip("."),
                    "size_bytes": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                })
        return sorted(exports, key=lambda x: x["modified"], reverse=True)

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
