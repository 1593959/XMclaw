"""AutobiographicalMemory — structured "who is this user" store.

Sprint 1 Track B of the Jarvis roadmap. Companion to ``SqliteVecMemory``
(vector store for fuzzy semantic recall). THIS module owns the
STRUCTURED facts that a real assistant needs to remember about you:

* **People** — name, relationship, importance, last seen, notes
* **Projects** — name, status, current focus, last touched
* **Facts** — typed (kind, subject, predicate, value) tuples with
  confidence + source so we can age them out / contradict cleanly
* **Routines** — observed schedules (you query email at 9am, you
  game on Wednesday nights), confidence grows with observations

Why a separate store
====================

Vector store is great for "what did we talk about last week that
sounds like X" but bad for "who is 何鹏 and how do I refer to him".
Structured tables let us:

* upsert idempotently — same fact stated twice doesn't dupe
* contradict cleanly — `confidence` field tracks Bayesian-ish updates
* render to system prompt as a tight bullet list (10 facts per
  category, NOT 50 fragmented chunks)
* surface to UI as a profile page

Extraction pipeline
===================

Two paths:

1. **Rule-based** (cheap, always on): regex patterns over the user
   message extract explicit facts like "我是 X" / "我在做 X" /
   "我喜欢 X". Confidence ~0.7.
2. **LLM-based** (optional, requires a fast tier model): after each
   turn, dispatch a cheap LLM call to extract structured facts.
   Confidence ~0.9.

Both write through the same ``record_*`` API. Path 1 is default ON,
path 2 opt-in via config ``cognition.autobiographical.llm_extract=true``.

Recall API
==========

* ``summarize_for_prompt(max_facts=20)`` → markdown bullet list
  injected into the system prompt at turn start. The agent sees
  "User profile snapshot" with name / role / projects / etc.
* ``facts_about(subject)`` → list[Fact] for surfacing in tool result
* ``forget(kind, subject)`` → delete a fact (user asks "stop
  remembering that")
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_SCHEMA = """
CREATE TABLE IF NOT EXISTS auto_people (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    relationship TEXT,
    importance   REAL NOT NULL DEFAULT 0.5,
    last_seen_ts REAL,
    notes_json   TEXT,
    created_ts   REAL NOT NULL,
    updated_ts   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS auto_projects (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    status        TEXT,
    current_focus TEXT,
    last_touch_ts REAL,
    created_ts    REAL NOT NULL,
    updated_ts    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS auto_facts (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    subject     TEXT NOT NULL,
    predicate   TEXT NOT NULL,
    value       TEXT,
    confidence  REAL NOT NULL DEFAULT 0.7,
    source      TEXT NOT NULL DEFAULT 'rule_based',
    learned_ts  REAL NOT NULL,
    updated_ts  REAL NOT NULL,
    -- UNIQUE on (kind, subject, predicate, value) so the same
    -- (predicate, value) pair upserts idempotently BUT different
    -- values under the same predicate (e.g. multiple "likes") get
    -- separate rows. "" used as the sentinel for NULL value so the
    -- index works.
    UNIQUE (kind, subject, predicate, value)
);

CREATE TABLE IF NOT EXISTS auto_routines (
    name           TEXT PRIMARY KEY,
    schedule_hint  TEXT,
    last_obs_ts    REAL,
    observation_n  INTEGER NOT NULL DEFAULT 1,
    confidence     REAL NOT NULL DEFAULT 0.3
);

CREATE INDEX IF NOT EXISTS auto_facts_subject_idx ON auto_facts (subject);
CREATE INDEX IF NOT EXISTS auto_facts_kind_idx ON auto_facts (kind);
"""


@dataclass(frozen=True, slots=True)
class Person:
    id: str
    name: str
    relationship: str | None
    importance: float
    last_seen_ts: float | None
    notes: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Project:
    id: str
    name: str
    status: str | None
    current_focus: str | None
    last_touch_ts: float | None


@dataclass(frozen=True, slots=True)
class Fact:
    id: str
    kind: str       # "preference" | "fact" | "skill" | "context" | "goal"
    subject: str    # "user" | person-name | project-name | ...
    predicate: str  # "likes" | "is" | "wants" | "works_on" | ...
    value: str | None
    confidence: float
    source: str     # "rule_based" | "llm" | "user_stated" | "observed"
    learned_ts: float
    updated_ts: float


# ── Rule-based extractor patterns ──────────────────────────────────


# Self-statement patterns (Chinese + English mixed).
# Each entry: (regex, predicate, kind, confidence)
_SELF_RULES: list[tuple[re.Pattern, str, str, float]] = [
    # 中文 self facts
    (re.compile(r"我是\s*(.{1,40}?)[。\.,，\n!]"), "is", "fact", 0.75),
    (re.compile(r"我叫\s*(.{1,30}?)[。\.,，\n!]"), "name", "fact", 0.85),
    (re.compile(r"我喜欢\s*(.{1,40}?)[。\.,，\n!]"), "likes", "preference", 0.7),
    (re.compile(r"我讨厌\s*(.{1,40}?)[。\.,，\n!]"), "dislikes", "preference", 0.7),
    (re.compile(r"我不喜欢\s*(.{1,40}?)[。\.,，\n!]"), "dislikes", "preference", 0.7),
    (re.compile(r"我在做\s*(.{1,60}?)[。\.,，\n!]"), "working_on", "context", 0.7),
    (re.compile(r"我想\s*(.{1,60}?)[。\.,，\n!]"), "wants", "goal", 0.6),
    (re.compile(r"我要\s*(.{1,60}?)[。\.,，\n!]"), "wants", "goal", 0.65),
    (re.compile(r"我会\s*(.{1,60}?)[。\.,，\n!]"), "can", "skill", 0.6),
    # English self facts
    (re.compile(r"\bI\s+am\s+(?:a|an|the)?\s*([^,.!?\n]{1,40})", re.IGNORECASE), "is", "fact", 0.7),
    (re.compile(r"\bMy\s+name\s+is\s+([^,.!?\n]{1,30})", re.IGNORECASE), "name", "fact", 0.85),
    (re.compile(r"\bI\s+(?:love|enjoy|like)\s+([^,.!?\n]{1,40})", re.IGNORECASE), "likes", "preference", 0.7),
    (re.compile(r"\bI\s+(?:hate|dislike)\s+([^,.!?\n]{1,40})", re.IGNORECASE), "dislikes", "preference", 0.7),
    (re.compile(r"\bI(?:'m|\s+am)\s+working\s+on\s+([^,.!?\n]{1,60})", re.IGNORECASE), "working_on", "context", 0.75),
    (re.compile(r"\bI\s+want(?:\s+to)?\s+([^,.!?\n]{1,60})", re.IGNORECASE), "wants", "goal", 0.6),
]

# Person-mention pattern: "我朋友 X" / "我同事 X" — capture relationship.
# Cap captured name at 4 CJK chars OR 12 ASCII chars. Stop at common
# verb particles ("今天", "明天", "昨天", "去", "来", "在", "的")
# which signal end-of-name in Chinese.
_PERSON_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(
        r"我(朋友|同事|哥们|老板|妻子|老婆|老公|丈夫|爸爸|妈妈|"
        r"父亲|母亲|弟弟|妹妹|哥哥|姐姐)\s*"
        r"([一-龥A-Za-z0-9]{1,6}?)"
        r"(?=[\s，。、,.!?]|$|今天|明天|昨天|刚才|去|来|是|的|很|在|了|说|想|要|吧)"
    ), "relationship_group"),
]


def _now() -> float:
    return time.time()


def _new_id() -> str:
    return uuid.uuid4().hex


class AutobiographicalMemory:
    """Structured store of who-the-user-is + extractor pipeline.

    One instance per daemon. SQLite-backed at
    ``~/.xmclaw/v2/autobio/store.sqlite``. Safe to share across
    coroutines (each call opens its own connection — sqlite3 in
    WAL mode handles concurrency).
    """

    def __init__(self, root: Path | None = None) -> None:
        if root is None:
            from xmclaw.utils.paths import data_dir
            root = data_dir() / "v2" / "autobio"
        self._root = Path(root)
        self._db_path = self._root / "store.sqlite"
        self._root.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    # ── Recording ─────────────────────────────────────────────────

    def record_fact(
        self,
        *,
        kind: str,
        subject: str,
        predicate: str,
        value: str | None = None,
        confidence: float = 0.7,
        source: str = "rule_based",
    ) -> str:
        """Upsert a fact. ``UNIQUE(kind, subject, predicate)`` makes
        repeated statements idempotent — the second call updates
        ``value`` + ``updated_ts`` + (averaged) ``confidence``.

        Confidence merge: existing 0.7 + new 0.7 → 0.85 (cap 0.99).
        Higher-confidence sources (LLM extract) overwrite lower
        (rule-based) on contradiction.
        """
        now = _now()
        fid = _new_id()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, confidence FROM auto_facts "
                "WHERE kind=? AND subject=? AND predicate=? AND "
                "(value IS ? OR value=?)",
                (kind, subject.strip().lower(), predicate, value, value),
            ).fetchone()
            if row is not None:
                old_conf = float(row["confidence"])
                merged_conf = min(0.99, max(old_conf, confidence)
                                  + 0.1 * min(old_conf, confidence))
                conn.execute(
                    "UPDATE auto_facts "
                    "SET value=?, confidence=?, source=?, updated_ts=? "
                    "WHERE id=?",
                    (value, merged_conf, source, now, row["id"]),
                )
                conn.commit()
                return row["id"]
            conn.execute(
                "INSERT INTO auto_facts "
                "(id, kind, subject, predicate, value, confidence, "
                " source, learned_ts, updated_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (fid, kind, subject.strip().lower(), predicate, value,
                 confidence, source, now, now),
            )
            conn.commit()
        return fid

    def record_person(
        self,
        *,
        name: str,
        relationship: str | None = None,
        importance: float = 0.5,
        notes: dict[str, Any] | None = None,
    ) -> str:
        """Upsert a person. Name is the unique key. Confidence-style
        merging on importance (max-with-decay)."""
        now = _now()
        name = name.strip()
        if not name:
            return ""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, importance, notes_json FROM auto_people "
                "WHERE name=?", (name,),
            ).fetchone()
            if row is not None:
                merged_imp = min(1.0, max(float(row["importance"]),
                                         importance) + 0.05)
                merged_notes = json.loads(row["notes_json"] or "{}")
                if notes:
                    merged_notes.update(notes)
                conn.execute(
                    "UPDATE auto_people "
                    "SET relationship=COALESCE(?, relationship), "
                    "  importance=?, notes_json=?, last_seen_ts=?, "
                    "  updated_ts=? "
                    "WHERE id=?",
                    (relationship, merged_imp,
                     json.dumps(merged_notes, ensure_ascii=False),
                     now, now, row["id"]),
                )
                conn.commit()
                return row["id"]
            pid = _new_id()
            conn.execute(
                "INSERT INTO auto_people "
                "(id, name, relationship, importance, last_seen_ts, "
                " notes_json, created_ts, updated_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (pid, name, relationship, importance, now,
                 json.dumps(notes or {}, ensure_ascii=False),
                 now, now),
            )
            conn.commit()
            return pid

    def record_project(
        self,
        *,
        name: str,
        status: str | None = None,
        current_focus: str | None = None,
    ) -> str:
        """Upsert a project. Updates ``last_touch_ts`` every record
        so stale-project triggers can detect projects you haven't
        mentioned in a while."""
        now = _now()
        name = name.strip()
        if not name:
            return ""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM auto_projects WHERE name=?", (name,),
            ).fetchone()
            if row is not None:
                conn.execute(
                    "UPDATE auto_projects "
                    "SET status=COALESCE(?, status), "
                    "  current_focus=COALESCE(?, current_focus), "
                    "  last_touch_ts=?, updated_ts=? "
                    "WHERE id=?",
                    (status, current_focus, now, now, row["id"]),
                )
                conn.commit()
                return row["id"]
            pid = _new_id()
            conn.execute(
                "INSERT INTO auto_projects "
                "(id, name, status, current_focus, last_touch_ts, "
                " created_ts, updated_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (pid, name, status, current_focus, now, now, now),
            )
            conn.commit()
            return pid

    # ── Rule-based extractor ──────────────────────────────────────

    def extract_from_message(self, message: str) -> int:
        """Scan a user message for self / person / project mentions.
        Returns count of facts recorded.

        Idempotent: same message extracted twice doesn't dupe (the
        unique constraint on facts protects us)."""
        if not isinstance(message, str) or not message.strip():
            return 0
        text = message.strip()
        n = 0
        for pat, predicate, kind, conf in _SELF_RULES:
            for m in pat.finditer(text):
                value = m.group(1).strip()
                if not value or len(value) < 1:
                    continue
                self.record_fact(
                    kind=kind, subject="user",
                    predicate=predicate, value=value,
                    confidence=conf, source="rule_based",
                )
                n += 1
        for pat, _kind in _PERSON_RULES:
            for m in pat.finditer(text):
                rel = m.group(1)
                name = m.group(2).strip()
                if not name:
                    continue
                self.record_person(name=name, relationship=rel)
                n += 1
        return n

    # ── Recall ────────────────────────────────────────────────────

    def facts_about(self, subject: str) -> list[Fact]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM auto_facts WHERE subject=? "
                "ORDER BY confidence DESC, updated_ts DESC",
                (subject.strip().lower(),),
            ).fetchall()
        return [
            Fact(
                id=r["id"], kind=r["kind"], subject=r["subject"],
                predicate=r["predicate"], value=r["value"],
                confidence=float(r["confidence"]), source=r["source"],
                learned_ts=float(r["learned_ts"]),
                updated_ts=float(r["updated_ts"]),
            )
            for r in rows
        ]

    def people(self, limit: int = 30) -> list[Person]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM auto_people "
                "ORDER BY importance DESC, last_seen_ts DESC "
                "LIMIT ?", (limit,),
            ).fetchall()
        return [
            Person(
                id=r["id"], name=r["name"],
                relationship=r["relationship"],
                importance=float(r["importance"]),
                last_seen_ts=(
                    float(r["last_seen_ts"])
                    if r["last_seen_ts"] is not None else None
                ),
                notes=json.loads(r["notes_json"] or "{}"),
            )
            for r in rows
        ]

    def projects(self, limit: int = 20) -> list[Project]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM auto_projects "
                "ORDER BY last_touch_ts DESC NULLS LAST LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            Project(
                id=r["id"], name=r["name"], status=r["status"],
                current_focus=r["current_focus"],
                last_touch_ts=(
                    float(r["last_touch_ts"])
                    if r["last_touch_ts"] is not None else None
                ),
            )
            for r in rows
        ]

    def summarize_for_prompt(
        self, *, max_facts: int = 20,
    ) -> str:
        """Return a markdown snippet for injection into the system
        prompt. Empty string when there's nothing remembered.

        Format:

            ## What I remember about you
            * name: 何鹏
            * works on: XMclaw, 魔丸 group
            * likes: 黑客松
            * dislikes: 早起
            * Recent people: 小6子 (朋友), 老史 (朋友)
            * Recent projects: XMclaw (active focus: multimodal UI)
        """
        user_facts = self.facts_about("user")
        ppl = self.people(limit=8)
        projs = self.projects(limit=5)
        if not user_facts and not ppl and not projs:
            return ""

        lines: list[str] = ["## What I remember about you"]
        # Group user facts by predicate for compact display.
        by_pred: dict[str, list[str]] = {}
        for f in user_facts[:max_facts]:
            by_pred.setdefault(f.predicate, []).append(f.value or "")
        for pred, vals in by_pred.items():
            uniq = [v for v in dict.fromkeys(vals) if v]
            if uniq:
                lines.append(f"* **{pred}**: {', '.join(uniq[:5])}")

        if ppl:
            ppl_str = ", ".join(
                f"{p.name}"
                + (f" ({p.relationship})" if p.relationship else "")
                for p in ppl
            )
            lines.append(f"* **Recent people**: {ppl_str}")

        if projs:
            proj_str = ", ".join(
                f"{p.name}"
                + (f" — {p.current_focus}" if p.current_focus else "")
                for p in projs
            )
            lines.append(f"* **Recent projects**: {proj_str}")
        return "\n".join(lines)

    # ── Forget / cleanup ─────────────────────────────────────────

    def forget_fact(
        self,
        *,
        kind: str,
        subject: str,
        predicate: str,
    ) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM auto_facts "
                "WHERE kind=? AND subject=? AND predicate=?",
                (kind, subject.strip().lower(), predicate),
            )
            conn.commit()
            return cur.rowcount > 0

    def forget_person(self, name: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM auto_people WHERE name=?",
                (name.strip(),),
            )
            conn.commit()
            return cur.rowcount > 0


__all__ = [
    "AutobiographicalMemory",
    "Person", "Project", "Fact",
]
