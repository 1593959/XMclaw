"""SQLite storage for structured memory."""
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


class SQLiteStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self._init_tables()

    def _init_tables(self) -> None:
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS insights (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                type TEXT,
                title TEXT,
                description TEXT,
                source TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS genes (
                id TEXT PRIMARY KEY,
                agent_id TEXT,
                name TEXT,
                description TEXT,
                trigger TEXT,
                trigger_type TEXT DEFAULT 'keyword',
                action TEXT,
                priority INTEGER DEFAULT 5,
                enabled INTEGER DEFAULT 1,
                intents TEXT DEFAULT '[]',
                regex_pattern TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS skills (
                id TEXT PRIMARY KEY,
                agent_id TEXT,
                name TEXT,
                category TEXT,
                version TEXT,
                path TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                session_data TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS skill_stats (
                name TEXT PRIMARY KEY,
                usage_count INTEGER DEFAULT 0,
                success_rate REAL DEFAULT 0.8,
                last_used TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        self.conn.commit()
        # Migrate existing genes table: add missing columns if absent
        self._migrate_genes_schema()

    def insert_insight(self, agent_id: str, insight: dict) -> None:
        self.conn.execute(
            "INSERT INTO insights (agent_id, type, title, description, source) VALUES (?, ?, ?, ?, ?)",
            (
                agent_id,
                insight.get("type"),
                insight.get("title"),
                insight.get("description"),
                insight.get("source"),
            ),
        )
        self.conn.commit()

    def get_insights(self, agent_id: str, limit: int = 50) -> list[dict[str, Any]]:
        cursor = self.conn.execute(
            "SELECT * FROM insights WHERE agent_id = ? ORDER BY created_at DESC LIMIT ?",
            (agent_id, limit),
        )
        return [dict(row) for row in cursor.fetchall()]

    def _migrate_genes_schema(self) -> None:
        """Add missing columns to an existing genes table (no-op if already present)."""
        cols_to_add = [
            ("trigger_type", "TEXT DEFAULT 'keyword'"),
            ("priority", "INTEGER DEFAULT 5"),
            ("enabled", "INTEGER DEFAULT 1"),
            ("intents", "TEXT DEFAULT '[]'"),
            ("regex_pattern", "TEXT DEFAULT ''"),
        ]
        try:
            for col_name, col_def in cols_to_add:
                self.conn.execute(f"ALTER TABLE genes ADD COLUMN {col_name} {col_def}")
            self.conn.commit()
        except Exception:
            pass  # Column already exists or table has no rows

    def insert_gene(self, agent_id: str, gene: dict) -> None:
        import json
        intents_val = gene.get("intents")
        if isinstance(intents_val, list):
            intents_val = json.dumps(intents_val)
        self.conn.execute(
            "INSERT OR REPLACE INTO genes "
            "(id, agent_id, name, description, trigger, trigger_type, action, priority, enabled, intents, regex_pattern) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                gene["id"],
                agent_id,
                gene.get("name"),
                gene.get("description"),
                gene.get("trigger"),
                gene.get("trigger_type", "keyword"),
                gene.get("action"),
                gene.get("priority", 5),
                1 if gene.get("enabled", True) else 0,
                intents_val or "[]",
                gene.get("regex_pattern", ""),
            ),
        )
        self.conn.commit()

    def get_genes(self, agent_id: str) -> list[dict[str, Any]]:
        cursor = self.conn.execute(
            "SELECT * FROM genes WHERE agent_id = ? ORDER BY created_at DESC",
            (agent_id,),
        )
        return [dict(row) for row in cursor.fetchall()]

    def insert_skill(self, agent_id: str, skill: dict) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO skills (id, agent_id, name, category, version, path) VALUES (?, ?, ?, ?, ?, ?)",
            (
                skill["id"],
                agent_id,
                skill.get("name"),
                skill.get("category"),
                skill.get("version"),
                skill.get("path"),
            ),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
