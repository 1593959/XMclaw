"""Entity layer — Wave-32+ (2026-05-18).

Steps the user explicitly asked for: "都做" → catch up with mature
vector DBs by introducing a canonical entity layer above free-text
facts. The graph view's "cluster keeps splitting / merging" pain
comes from facts being **disconnected strings**: when the LLM
extractor writes "陪玩店账号为admin" and later "陪玩店网站地址为
https://pw310...", these are SEPARATE facts with no shared
identifier — only a regex / cosine bridge can connect them, and
that bridge is fragile.

This module gives every fact a **set of canonical entities** it
mentions. The entities live in a process-level store keyed by
their canonical form (URL lowercased + path normalized, CJK
bi-gram, ASCII identifier ≥ 4 chars). A reverse index
``_entity_to_facts: dict[entity_id, set[fact_id]]`` lets the
SAME_TOPIC scan find "all facts that mention entity X" in O(1).

Initial implementation is **in-memory only** — rebuilt from a
linear scan over the fact store at startup or first access. No
schema migration; persistence comes in a follow-up.

Layered design:

  * :func:`canonicalize` — string → canonical form (URL norm, lower
    ascii, strip punctuation). Pure.
  * :func:`extract_entity_mentions` — text → list of (canonical,
    surface, type). Pure. Reuses regex patterns from service.py
    but emits structured ``EntityMention`` records, not raw tokens.
  * :class:`EntityStore` — register / lookup / reverse-index.
    Process-singleton via :func:`get_entity_store`.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


# ── Canonical form ────────────────────────────────────────────────────


# URL normalization: strip protocol's case, default port, trailing
# slash. Two URLs that point at the same resource should canonicalize
# to the same string.
_URL_RE = re.compile(r"https?://[\w\-.:/?=&%+#~]+", re.IGNORECASE)
_TRAILING_SLASH_RE = re.compile(r"/+$")


def _canonicalize_url(raw: str) -> str:
    """Lowercase scheme + host, strip default ports, drop trailing
    slashes from the path. Doesn't reorder query params (rare in
    practice for the fact domain)."""
    s = raw.strip()
    if not s:
        return ""
    s = s.lower()
    # Strip default ports.
    s = re.sub(r":80(/|$)", r"\1", s)
    s = re.sub(r":443(/|$)", r"\1", s)
    s = _TRAILING_SLASH_RE.sub("", s)
    return s


def canonicalize(text: str, *, type_hint: str = "") -> str:
    """Normalize raw text into a stable canonical form. The same
    semantic entity should always produce the same canonical string
    regardless of surface variations.

    ``type_hint`` (when known) routes to specialized canonicalizers
    — URLs care about case+port+slash; ASCII identifiers just
    lowercase; CJK noun phrases pass through (no case to lower)."""
    if not text:
        return ""
    s = text.strip()
    if not s:
        return ""
    if type_hint == "url":
        return _canonicalize_url(s)
    # Detect URL by content if no hint.
    if s.lower().startswith(("http://", "https://")):
        return _canonicalize_url(s)
    # ASCII identifier — lowercase, strip wrapping punctuation.
    if all(ord(c) < 128 for c in s):
        return s.lower().strip(".,;:!?()[]{}'\"`")
    # CJK — pass through (already case-insensitive)
    return s


# ── Mention extraction ────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class EntityMention:
    """One detected entity inside a fact's text.

    Multiple mentions can canonicalize to the same entity_id (the
    URL ``https://pw310.wxselling.com`` and the bare hostname
    ``pw310.wxselling.com`` produce two mentions, both pointing at
    the same canonical entity)."""

    canonical: str
    surface: str
    type: str  # "url" | "ascii_id" | "cjk_bigram" | "domain"


# Patterns used to extract mentions. Distinct from the regex patterns
# in service.py — those emit raw tokens for the cheap token-bridge,
# these emit STRUCTURED records suitable for the entity store.
_PATTERNS_URL = re.compile(r"https?://[\w\-.:/?=&%+#~]+")
# Domain / ASCII ID patterns: \b doesn't work between CJK and ASCII
# (both are word chars in Unicode), so we anchor on character class
# transitions explicitly. ``[A-Za-z]`` at the start of the ASCII id
# pattern means the regex engine won't match starting from the
# middle of another ASCII identifier — the previous char would be
# alphabetic and the engine slides past.
_PATTERNS_DOMAIN = re.compile(r"([\w\-]{3,}\.[\w\-]{2,}(?:\.[\w\-]{2,})*)")
_PATTERNS_ASCII_ID = re.compile(r"[A-Za-z][\w\-]{3,}")
_PATTERNS_CJK_RUN = re.compile(r"[一-龥]{2,}")

# Stopwords identical to the service.py set — kept here so changes
# stay consistent. (Could share via import; kept local to keep this
# module self-contained for the tests.)
_STOPWORDS = frozenset({
    "我们", "你们", "他们", "可以", "现在", "今天", "昨天", "明天",
    "这个", "那个", "什么", "怎么", "为什么", "已经", "因为",
    "所以", "如果", "或者", "然后", "之前", "之后", "里面", "外面",
    "需要", "希望", "应该", "可能", "应当", "记得",
    "用户", "助手",
    "this", "that", "with", "from", "have", "been", "will",
    "should", "would", "could", "must", "into", "what", "where",
    "when", "why", "how", "the", "and", "for", "are", "but",
    "not", "you", "can", "all", "any", "true", "false",
    "none", "null", "http", "https",
})


def extract_entity_mentions(text: str) -> list[EntityMention]:
    """Walk ``text``, emit structured EntityMention records for every
    distinct entity reference. Order: URLs first (most distinctive),
    then domains, then ASCII identifiers, then CJK bi-grams.

    The same surface form can produce mentions at multiple levels —
    e.g. ``https://pw310.wxselling.com`` extracts:
      * URL mention (full URL)
      * domain mention (``pw310.wxselling.com``)
    Same canonical for the URL itself + a sibling canonical for the
    bare domain. Downstream the entity store merges by canonical
    so duplicates collapse.
    """
    if not text:
        return []
    seen: set[str] = set()
    out: list[EntityMention] = []

    def _add(canonical: str, surface: str, type_: str) -> None:
        if not canonical or canonical in seen:
            return
        if canonical in _STOPWORDS:
            return
        seen.add(canonical)
        out.append(EntityMention(canonical=canonical, surface=surface, type=type_))

    for m in _PATTERNS_URL.finditer(text):
        raw = m.group(0)
        _add(canonicalize(raw, type_hint="url"), raw, "url")
    for m in _PATTERNS_DOMAIN.finditer(text):
        raw = m.group(0)
        canon = canonicalize(raw)
        # Skip if this is just a sub-string of a URL we already
        # registered (same domain extracted twice).
        if any(raw in e.surface for e in out if e.type == "url"):
            continue
        _add(canon, raw, "domain")
    for m in _PATTERNS_ASCII_ID.finditer(text):
        raw = m.group(0)
        if len(raw) < 4:
            continue
        canon = canonicalize(raw)
        _add(canon, raw, "ascii_id")
    for m in _PATTERNS_CJK_RUN.finditer(text):
        run = m.group(0)
        if len(run) < 2:
            continue
        if len(run) == 2:
            _add(run, run, "cjk_bigram")
        else:
            for i in range(len(run) - 1):
                bg = run[i:i + 2]
                _add(bg, bg, "cjk_bigram")
    return out


def entity_id_for(canonical: str) -> str:
    """Stable id derived from canonical form. SHA1 prefix is enough
    — collision probability over a single user's lifetime fact set
    is negligible. Same canonical → same id, across runs, across
    machines. Stable IDs unlock everything downstream:
    deterministic cluster ids, idempotent topic names, reproducible
    relationship debugging."""
    return "e_" + hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:14]


# ── EntityStore ───────────────────────────────────────────────────────


@dataclass
class Entity:
    """An entity tracked in the process-level store.

    ``surface_forms`` accumulates every variant we've seen written
    for this canonical entity — useful for the UI when we want to
    show "this entity has been called X, Y, Z" without the user
    having to expand 10 fact rows. Capped at 5 to keep memory
    bounded."""

    id: str
    canonical: str
    type: str
    surface_forms: tuple[str, ...] = ()
    first_seen_at: float = field(default_factory=time.time)
    last_seen_at: float = field(default_factory=time.time)
    mentions_count: int = 0


class EntityStore:
    """In-memory entity store with a reverse index.

    Thread-safe via a single coarse lock — entity ops happen on the
    fact-write path which is already serialized per-session at the
    memory_service level, so contention is minimal."""

    def __init__(self) -> None:
        self._entities: dict[str, Entity] = {}
        # Reverse index: entity_id → set of fact_ids that mention it.
        # The bridge logic looks up "what other facts mention X" in
        # O(1) instead of doing an N-way text scan.
        self._entity_to_facts: dict[str, set[str]] = {}
        # Forward index: fact_id → set of entity_ids it mentions.
        # Used to clean up the reverse index on fact deletion.
        self._fact_to_entities: dict[str, set[str]] = {}
        self._lock = threading.RLock()

    # ── Register ────────────────────────────────────────────────────

    def register_mention(
        self, fact_id: str, mention: EntityMention,
    ) -> str:
        """Idempotent: upserts the entity + links it to the fact.
        Returns the entity_id."""
        if not mention.canonical:
            return ""
        eid = entity_id_for(mention.canonical)
        now = time.time()
        with self._lock:
            ent = self._entities.get(eid)
            if ent is None:
                ent = Entity(
                    id=eid,
                    canonical=mention.canonical,
                    type=mention.type,
                    surface_forms=(mention.surface,),
                    first_seen_at=now,
                    last_seen_at=now,
                    mentions_count=1,
                )
                self._entities[eid] = ent
            else:
                if mention.surface not in ent.surface_forms:
                    forms = ent.surface_forms + (mention.surface,)
                    if len(forms) > 5:
                        forms = forms[-5:]
                    ent.surface_forms = forms
                ent.last_seen_at = now
                ent.mentions_count += 1
            self._entity_to_facts.setdefault(eid, set()).add(fact_id)
            self._fact_to_entities.setdefault(fact_id, set()).add(eid)
        return eid

    def register_fact_text(self, fact_id: str, text: str) -> list[str]:
        """Extract + register all entities in ``text`` for one fact.
        Returns the list of entity_ids. Safe to call repeatedly with
        the same (fact_id, text) — idempotent at the entity level."""
        ids: list[str] = []
        for m in extract_entity_mentions(text):
            eid = self.register_mention(fact_id, m)
            if eid:
                ids.append(eid)
        return ids

    # ── Query ───────────────────────────────────────────────────────

    def entities_for_fact(self, fact_id: str) -> list[Entity]:
        """Return the entities a fact mentions. Snapshot — safe to
        iterate after release."""
        with self._lock:
            ids = list(self._fact_to_entities.get(fact_id, ()))
            return [
                self._entities[i] for i in ids if i in self._entities
            ]

    def facts_for_entity(self, entity_id: str) -> set[str]:
        """O(1) lookup: which facts mention this entity. Returns a
        copy so the caller can mutate freely."""
        with self._lock:
            return set(self._entity_to_facts.get(entity_id, ()))

    def shared_entities(
        self, fact_a_id: str, fact_b_id: str,
    ) -> set[str]:
        """Return the entity_ids both facts mention. The single most
        useful query for the SAME_TOPIC bridge — if two facts share
        entities, they're almost certainly about the same topic."""
        with self._lock:
            a = self._fact_to_entities.get(fact_a_id, set())
            b = self._fact_to_entities.get(fact_b_id, set())
            return a & b

    def co_mentioned_facts(
        self, fact_id: str, *, exclude: Iterable[str] = (),
    ) -> set[str]:
        """For a given fact, return all OTHER facts that share at
        least one entity with it. The natural candidate set for
        SAME_TOPIC bridging."""
        exclude_set = set(exclude) | {fact_id}
        result: set[str] = set()
        with self._lock:
            my_entities = self._fact_to_entities.get(fact_id, set())
            for eid in my_entities:
                for other in self._entity_to_facts.get(eid, set()):
                    if other not in exclude_set:
                        result.add(other)
        return result

    # ── Lifecycle ────────────────────────────────────────────────────

    def forget_fact(self, fact_id: str) -> None:
        """Clean up the index when a fact is deleted / superseded."""
        with self._lock:
            eids = self._fact_to_entities.pop(fact_id, set())
            for eid in eids:
                facts = self._entity_to_facts.get(eid)
                if facts is None:
                    continue
                facts.discard(fact_id)
                if not facts:
                    # Entity with no remaining mentions — drop it +
                    # the reverse-index entry.
                    self._entity_to_facts.pop(eid, None)
                    self._entities.pop(eid, None)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "entities": len(self._entities),
                "facts_indexed": len(self._fact_to_entities),
                "reverse_links": sum(
                    len(s) for s in self._entity_to_facts.values()
                ),
            }

    def clear(self) -> None:
        """Test helper. Drops all state."""
        with self._lock:
            self._entities.clear()
            self._entity_to_facts.clear()
            self._fact_to_entities.clear()

    # ── Persistence (Wave-32+) ───────────────────────────────────────

    # JSON shape carries a version so future schema migrations can
    # detect + drop / migrate stale dumps. Bumping the version
    # invalidates the load on next startup — fine, the backfill
    # path repopulates from facts.
    _PERSIST_VERSION: int = 1

    def to_dict(self) -> dict:
        """Snapshot the in-memory state into a serializable dict.
        Caller is responsible for writing it to disk."""
        with self._lock:
            return {
                "v": self._PERSIST_VERSION,
                "entities": {
                    eid: {
                        "canonical": e.canonical,
                        "type": e.type,
                        "surface_forms": list(e.surface_forms),
                        "first_seen_at": e.first_seen_at,
                        "last_seen_at": e.last_seen_at,
                        "mentions_count": e.mentions_count,
                    }
                    for eid, e in self._entities.items()
                },
                # Reverse index — list-not-set so JSON serializes it.
                "entity_to_facts": {
                    eid: sorted(facts)
                    for eid, facts in self._entity_to_facts.items()
                },
                "fact_to_entities": {
                    fid: sorted(eids)
                    for fid, eids in self._fact_to_entities.items()
                },
            }

    def load_dict(self, data: dict) -> int:
        """Inverse of :meth:`to_dict`. Returns the entity count
        loaded (0 on schema mismatch / empty dump)."""
        if not isinstance(data, dict):
            return 0
        if data.get("v") != self._PERSIST_VERSION:
            return 0
        ents = data.get("entities") or {}
        e2f = data.get("entity_to_facts") or {}
        f2e = data.get("fact_to_entities") or {}
        if not isinstance(ents, dict) or not isinstance(e2f, dict) or not isinstance(f2e, dict):
            return 0
        with self._lock:
            self._entities.clear()
            self._entity_to_facts.clear()
            self._fact_to_entities.clear()
            for eid, payload in ents.items():
                if not isinstance(payload, dict):
                    continue
                try:
                    self._entities[eid] = Entity(
                        id=eid,
                        canonical=str(payload.get("canonical") or ""),
                        type=str(payload.get("type") or ""),
                        surface_forms=tuple(payload.get("surface_forms") or ()),
                        first_seen_at=float(payload.get("first_seen_at") or 0),
                        last_seen_at=float(payload.get("last_seen_at") or 0),
                        mentions_count=int(payload.get("mentions_count") or 0),
                    )
                except (TypeError, ValueError):
                    continue
            for eid, facts in e2f.items():
                if isinstance(facts, list):
                    self._entity_to_facts[eid] = set(facts)
            for fid, eids in f2e.items():
                if isinstance(eids, list):
                    self._fact_to_entities[fid] = set(eids)
            return len(self._entities)

    def save_to(self, path: "Path") -> bool:
        """Write the index to ``path`` as JSON. Returns True on
        success. Atomic via write-to-temp + rename so a crash
        mid-write can't corrupt the persisted state."""
        import json
        try:
            payload = self.to_dict()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(path)
            return True
        except OSError:
            return False

    def load_from(self, path: "Path") -> int:
        """Read the index from ``path``. Returns entity count loaded;
        0 on missing-file / parse error / version mismatch."""
        import json
        try:
            text = path.read_text(encoding="utf-8")
            data = json.loads(text)
        except (OSError, ValueError):
            return 0
        return self.load_dict(data)

    async def rebuild_from_facts(self, vec_backend: "Any") -> dict:
        """One-shot backfill: scan every fact in ``vec_backend`` and
        re-register its text. Use after upgrading from a pre-entity
        version, or to recover a corrupted index.

        Returns ``{scanned, registered, errors}``. The store is
        cleared first so callers don't accumulate duplicates over
        repeated invocations."""
        scanned = 0
        registered = 0
        errors = 0
        try:
            facts = await vec_backend.search(None, where=None, limit=20000)
        except Exception:  # noqa: BLE001
            return {"scanned": 0, "registered": 0, "errors": 1}
        self.clear()
        for f in facts:
            scanned += 1
            text = getattr(f, "text", None) or ""
            fid = getattr(f, "id", None)
            superseded = getattr(f, "superseded_by", None)
            if superseded:
                continue
            if not fid or not text:
                continue
            try:
                ids = self.register_fact_text(fid, text)
                if ids:
                    registered += 1
            except Exception:  # noqa: BLE001
                errors += 1
        return {"scanned": scanned, "registered": registered, "errors": errors}


# ── Process-singleton accessor ────────────────────────────────────────


_default_store: EntityStore | None = None
_default_lock = threading.Lock()


def default_entity_store_path() -> "Path":
    """Where the singleton's index lives on disk. Honors
    ``XMC_DATA_DIR`` via :func:`xmclaw.utils.paths.data_dir` so the
    test suite's tmp-path overrides isolate properly."""
    from pathlib import Path
    try:
        from xmclaw.utils.paths import data_dir
        return data_dir() / "v2" / "entity_index.json"
    except Exception:  # noqa: BLE001
        return Path.home() / ".xmclaw" / "v2" / "entity_index.json"


def get_entity_store() -> EntityStore:
    """Lazy process-singleton. Build on first access so test paths
    that don't touch entities pay zero cost.

    On first build, attempts to load the persisted index from
    :func:`default_entity_store_path`. Failures (missing file,
    parse error, version mismatch) silently fall back to an empty
    store — the lifespan's backfill pass repopulates."""
    global _default_store
    if _default_store is None:
        with _default_lock:
            if _default_store is None:
                _default_store = EntityStore()
                try:
                    _default_store.load_from(default_entity_store_path())
                except Exception:  # noqa: BLE001
                    pass
    return _default_store


def reset_entity_store() -> None:
    """Test helper — drop the singleton so the next call rebuilds."""
    global _default_store
    with _default_lock:
        _default_store = None


__all__ = [
    "Entity",
    "EntityMention",
    "EntityStore",
    "canonicalize",
    "default_entity_store_path",
    "entity_id_for",
    "extract_entity_mentions",
    "get_entity_store",
    "reset_entity_store",
]
