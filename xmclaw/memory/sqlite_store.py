"""SQLite storage for structured memory."""
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


class SQLiteStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
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
                action TEXT,
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
        """)
        self.conn.commit()

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

    def insert_gene(self, agent_id: str, gene: dict) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO genes (id, agent_id, name, description, trigger, action) VALUES (?, ?, ?, ?, ?, ?)",
            (
                gene["id"],
                agent_id,
                gene.get("name"),
                gene.get("description"),
                gene.get("trigger"),
                gene.get("action"),
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
