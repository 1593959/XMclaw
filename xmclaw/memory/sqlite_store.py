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
        # WAL: concurrent reader/writer safety for the evolution pipeline.
        try:
            self.conn.execute("PRAGMA journal_mode=WAL;")
            self.conn.execute("PRAGMA synchronous=NORMAL;")
        except sqlite3.OperationalError:
            pass
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
            -- Evolution journal: every cycle is a first-class record with
            -- full lineage so meta-evaluation can close the loop.
            CREATE TABLE IF NOT EXISTS evolution_journal (
                cycle_id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                trigger TEXT,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                ended_at TIMESTAMP,
                inputs_json TEXT DEFAULT '{}',
                decisions_json TEXT DEFAULT '{}',
                artifacts_json TEXT DEFAULT '[]',
                verdict TEXT DEFAULT 'pending',
                reject_reason TEXT,
                metrics_json TEXT DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_journal_agent
                ON evolution_journal(agent_id, started_at DESC);
            -- One row per artifact per cycle. status drives whether skill_match
            -- or gene_match will see the artifact at runtime.
            CREATE TABLE IF NOT EXISTS evolution_artifact_lineage (
                artifact_id TEXT NOT NULL,
                cycle_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                parent_artifact_id TEXT,
                status TEXT NOT NULL DEFAULT 'shadow',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                matched_count INTEGER DEFAULT 0,
                helpful_count INTEGER DEFAULT 0,
                harmful_count INTEGER DEFAULT 0,
                PRIMARY KEY (artifact_id, cycle_id)
            );
            CREATE INDEX IF NOT EXISTS idx_lineage_cycle
                ON evolution_artifact_lineage(cycle_id);
            CREATE INDEX IF NOT EXISTS idx_lineage_status
                ON evolution_artifact_lineage(agent_id, kind, status);
            -- Phase E6: message-level human feedback. One row per turn_id;
            -- last write wins so users can flip 👍↔👎 without accumulating
            -- conflicting signals. Reflection prompts join on this table so
            -- the evolution loop is driven by real human verdicts, not only
            -- LLM self-assessment.
            CREATE TABLE IF NOT EXISTS user_feedback (
                agent_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                thumb TEXT NOT NULL,
                note TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (agent_id, turn_id)
            );
            CREATE INDEX IF NOT EXISTS idx_user_feedback_agent
                ON user_feedback(agent_id, created_at DESC);
        """)
        self.conn.commit()
        # Migrate existing genes table: add missing columns if absent
        self._migrate_genes_schema()
        # Phase E7: lineage rows need sha columns for git-level rollback.
        self._migrate_lineage_schema()

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

    def _migrate_lineage_schema(self) -> None:
        """Phase E7: record git commit SHAs for promote/rollback transitions.

        Each ALTER is run in its own savepoint so a pre-existing column
        doesn't abort the rest of the migration. Safe on fresh DBs — the
        table is created above without these columns and ALTER fills them in.
        """
        cols_to_add = [
            ("promote_commit_sha", "TEXT"),
            ("rollback_commit_sha", "TEXT"),
        ]
        for col_name, col_def in cols_to_add:
            try:
                self.conn.execute(
                    f"ALTER TABLE evolution_artifact_lineage "
                    f"ADD COLUMN {col_name} {col_def}"
                )
                self.conn.commit()
            except Exception:
                pass  # column already exists

    def lineage_set_commit_sha(
        self, artifact_id: str, column: str, sha: str,
    ) -> int:
        """Record a promote/rollback git SHA on the lineage row.

        ``column`` is whitelisted so this cannot be used as a generic
        arbitrary-column update. Returns rowcount so callers can detect
        a typoed artifact_id.
        """
        if column not in ("promote_commit_sha", "rollback_commit_sha"):
            raise ValueError(f"unknown column: {column}")
        cur = self.conn.execute(
            f"UPDATE evolution_artifact_lineage "
            f"SET {column} = ?, updated_at = CURRENT_TIMESTAMP "
            f"WHERE artifact_id = ?",
            (sha, artifact_id),
        )
        self.conn.commit()
        return cur.rowcount

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

    def get_skills(self, agent_id: str) -> list[dict[str, Any]]:
        """Return every skill row registered for this agent.

        Coherence checks (Phase E6) need the full live set, not a single
        lookup, so they can compare the proposed concept against every
        installed skill. The skills table only holds promoted artifacts —
        shadow/retired rows live in evolution_journal_artifacts — so the
        returned list IS the live set by construction.
        """
        cursor = self.conn.execute(
            "SELECT * FROM skills WHERE agent_id = ? ORDER BY created_at DESC",
            (agent_id,),
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_skill_by_concept_name(
        self, agent_id: str, concept_name: str,
    ) -> dict[str, Any] | None:
        """Look up a registered skill by its human-readable concept name.

        Used by the evolution engine's dedup guard: if the same insight has
        already produced a skill, don't forge a duplicate on the next cycle.
        Returns the full skill row (including `id`) or None.
        """
        cur = self.conn.execute(
            "SELECT * FROM skills WHERE agent_id = ? AND name = ? LIMIT 1",
            (agent_id, concept_name),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    # ── Evolution journal DAO ────────────────────────────────────────────────
    # All journal writes go through these methods so EvolutionJournal (see
    # xmclaw/evolution/journal.py) stays thin and infra-agnostic.

    def journal_insert_cycle(
        self, cycle_id: str, agent_id: str, trigger: str,
        inputs_json: str = "{}",
    ) -> None:
        self.conn.execute(
            "INSERT INTO evolution_journal "
            "(cycle_id, agent_id, trigger, inputs_json) VALUES (?, ?, ?, ?)",
            (cycle_id, agent_id, trigger, inputs_json),
        )
        self.conn.commit()

    def journal_update_cycle(self, cycle_id: str, **fields: Any) -> None:
        """Partial update. Accepts: inputs_json, decisions_json, artifacts_json,
        verdict, reject_reason, metrics_json, ended_at."""
        allowed = {
            "inputs_json", "decisions_json", "artifacts_json",
            "verdict", "reject_reason", "metrics_json", "ended_at",
        }
        cols = [k for k in fields if k in allowed]
        if not cols:
            return
        set_clause = ", ".join(f"{c} = ?" for c in cols)
        params = tuple(fields[c] for c in cols) + (cycle_id,)
        self.conn.execute(
            f"UPDATE evolution_journal SET {set_clause} WHERE cycle_id = ?",
            params,
        )
        self.conn.commit()

    def journal_get_cycle(self, cycle_id: str) -> dict[str, Any] | None:
        cur = self.conn.execute(
            "SELECT * FROM evolution_journal WHERE cycle_id = ?", (cycle_id,)
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def journal_list_cycles(
        self, agent_id: str, limit: int = 50,
    ) -> list[dict[str, Any]]:
        # rowid is monotonic and unique — reliable tiebreaker when multiple
        # cycles open within the same second (CURRENT_TIMESTAMP is sec-precision).
        cur = self.conn.execute(
            "SELECT * FROM evolution_journal WHERE agent_id = ? "
            "ORDER BY started_at DESC, rowid DESC LIMIT ?",
            (agent_id, limit),
        )
        return [dict(r) for r in cur.fetchall()]

    def journal_list_cycles_since(
        self, agent_id: str, window_seconds: int, limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Return cycle rows whose started_at falls within the last
        ``window_seconds``. Used by retrospective.cycle_summary (Phase E8)
        to bound the dashboard view to a useful time slice.

        ``window_seconds <= 0`` returns ``[]`` — a zero-or-negative window
        is vacuously empty and would otherwise collide with the fact that
        SQLite's ``datetime('now')`` has second precision, making
        `>= datetime('now', '-0 seconds')` match rows inserted the same
        second with CURRENT_TIMESTAMP.

        Uses SQLite's ``datetime('now', '-<n> seconds')`` so the filter
        stays correct whether started_at was inserted as CURRENT_TIMESTAMP
        or an explicit string — both compare lexicographically under ISO-8601.
        """
        if window_seconds <= 0:
            return []
        cur = self.conn.execute(
            "SELECT * FROM evolution_journal "
            "WHERE agent_id = ? "
            "AND started_at >= datetime('now', '-' || ? || ' seconds') "
            "ORDER BY started_at DESC, rowid DESC LIMIT ?",
            (agent_id, int(window_seconds), limit),
        )
        return [dict(r) for r in cur.fetchall()]

    def lineage_all(
        self, agent_id: str, kind: str | None = None, limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Every lineage row for this agent, regardless of status.

        Retrospective queries (Phase E8) aggregate across ALL statuses —
        ``lineage_active`` skips retired/rolled-back rows, which is the
        wrong default for a dashboard showing totals over time.
        """
        sql = (
            "SELECT * FROM evolution_artifact_lineage WHERE agent_id = ?"
        )
        params: list[Any] = [agent_id]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        cur = self.conn.execute(sql, tuple(params))
        return [dict(r) for r in cur.fetchall()]

    def lineage_by_status(
        self, agent_id: str, status: str, limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Lineage rows filtered to a single status, newest first.

        Used by retrospective.rollback_history to pull the last N
        rolled-back artifacts without loading the whole table.
        """
        cur = self.conn.execute(
            "SELECT * FROM evolution_artifact_lineage "
            "WHERE agent_id = ? AND status = ? "
            "ORDER BY updated_at DESC, rowid DESC LIMIT ?",
            (agent_id, status, int(limit)),
        )
        return [dict(r) for r in cur.fetchall()]

    def lineage_insert(
        self, artifact_id: str, cycle_id: str, agent_id: str, kind: str,
        parent_artifact_id: str | None = None, status: str = "shadow",
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO evolution_artifact_lineage "
            "(artifact_id, cycle_id, agent_id, kind, parent_artifact_id, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (artifact_id, cycle_id, agent_id, kind, parent_artifact_id, status),
        )
        self.conn.commit()

    def lineage_update_status(self, artifact_id: str, status: str) -> int:
        cur = self.conn.execute(
            "UPDATE evolution_artifact_lineage "
            "SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE artifact_id = ?",
            (status, artifact_id),
        )
        self.conn.commit()
        return cur.rowcount

    def lineage_increment(
        self, artifact_id: str, metric: str, delta: int = 1,
    ) -> int:
        # Whitelist to prevent SQL injection via metric name.
        if metric not in ("matched_count", "helpful_count", "harmful_count"):
            raise ValueError(f"unknown metric: {metric}")
        cur = self.conn.execute(
            f"UPDATE evolution_artifact_lineage "
            f"SET {metric} = {metric} + ?, updated_at = CURRENT_TIMESTAMP "
            f"WHERE artifact_id = ?",
            (delta, artifact_id),
        )
        self.conn.commit()
        return cur.rowcount

    def lineage_for_cycle(self, cycle_id: str) -> list[dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT * FROM evolution_artifact_lineage WHERE cycle_id = ? "
            "ORDER BY created_at",
            (cycle_id,),
        )
        return [dict(r) for r in cur.fetchall()]

    def lineage_for_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        cur = self.conn.execute(
            "SELECT * FROM evolution_artifact_lineage WHERE artifact_id = ? "
            "ORDER BY updated_at DESC LIMIT 1",
            (artifact_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def lineage_active(
        self, agent_id: str, kind: str | None = None,
        statuses: tuple[str, ...] = ("promoted", "shadow"),
    ) -> list[dict[str, Any]]:
        """Artifacts currently 'visible' to the runtime. Defaults to
        promoted+shadow; skill_match can filter further to promoted-only for
        auto-exec and include shadow for suggestions."""
        placeholders = ",".join("?" * len(statuses))
        params: list[Any] = [agent_id]
        sql = (
            f"SELECT * FROM evolution_artifact_lineage "
            f"WHERE agent_id = ? AND status IN ({placeholders})"
        )
        params.extend(statuses)
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY updated_at DESC"
        cur = self.conn.execute(sql, tuple(params))
        return [dict(r) for r in cur.fetchall()]

    # ── Phase E6: message-level human feedback ──────────────────────────────

    def upsert_user_feedback(
        self, agent_id: str, turn_id: str, thumb: str,
        note: str | None = None,
    ) -> None:
        """Record a 👍/👎 on a specific turn. Last write wins so a user
        flipping their mind overwrites, rather than stacking, feedback."""
        if thumb not in ("up", "down"):
            raise ValueError(f"thumb must be 'up' or 'down', got {thumb!r}")
        self.conn.execute(
            "INSERT INTO user_feedback (agent_id, turn_id, thumb, note) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(agent_id, turn_id) DO UPDATE SET "
            "thumb=excluded.thumb, note=excluded.note, "
            "created_at=CURRENT_TIMESTAMP",
            (agent_id, turn_id, thumb, note),
        )
        self.conn.commit()

    def get_user_feedback_by_turns(
        self, agent_id: str, turn_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        """Return {turn_id → row} for the given turn ids. Missing keys mean
        no feedback was recorded; callers should treat that as neutral."""
        if not turn_ids:
            return {}
        placeholders = ",".join("?" * len(turn_ids))
        cur = self.conn.execute(
            f"SELECT agent_id, turn_id, thumb, note, created_at "
            f"FROM user_feedback "
            f"WHERE agent_id = ? AND turn_id IN ({placeholders})",
            (agent_id, *turn_ids),
        )
        return {r["turn_id"]: dict(r) for r in cur.fetchall()}

    def get_recent_user_feedback(
        self, agent_id: str, limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Most-recent feedback rows for an agent, newest first. Used by
        dashboards and reflection summaries that want an at-a-glance view
        instead of turn-by-turn lookups."""
        cur = self.conn.execute(
            "SELECT agent_id, turn_id, thumb, note, created_at "
            "FROM user_feedback WHERE agent_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (agent_id, limit),
        )
        return [dict(r) for r in cur.fetchall()]

    def close(self) -> None:
        self.conn.close()
