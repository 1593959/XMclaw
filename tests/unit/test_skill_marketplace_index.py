"""Pin that the committed marketplace catalog stays loadable.

``docs/skill_marketplace_index.json`` is fetched at runtime by
``xmclaw skill list-marketplace`` (and the web UI's 技能商店 page) via
``MarketplaceIndex.from_dict``. A malformed edit would surface to the
user as an ``IndexFetchError`` / 404-style failure instead of at CI.
This test loads the real committed file through the real loader so a
bad edit fails the build, not the user.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from xmclaw.skills.marketplace import MarketplaceIndex, MarketplaceSkill


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
INDEX_PATH = REPO_ROOT / "docs" / "skill_marketplace_index.json"


def test_index_file_exists():
    """The marketplace command 404s without this file. It must exist
    in the repo so the GitHub-raw fetch resolves."""
    assert INDEX_PATH.is_file(), (
        f"{INDEX_PATH} missing — `xmclaw skill list-marketplace` "
        f"would 404. README references this catalog."
    )


def test_index_is_valid_json():
    raw = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    assert "skills" in raw
    assert isinstance(raw["skills"], list)


def test_index_loads_through_real_loader():
    """End-to-end: the SAME path the daemon uses must accept this
    file."""
    raw = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    idx = MarketplaceIndex.from_dict(raw)
    assert isinstance(idx, MarketplaceIndex)
    assert idx.version >= 1
    # Every skill entry must round-trip cleanly (id present, etc).
    for s in idx.skills:
        assert isinstance(s, MarketplaceSkill)
        assert s.id
        assert s.source  # an entry with no install source is useless


def test_index_search_empty_query_returns_all():
    raw = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    idx = MarketplaceIndex.from_dict(raw)
    assert len(idx.search("")) == len(idx.skills)


def test_every_skill_has_valid_trust_tier():
    """Trust tier gates the install confirmation UX. Only the two
    known tiers are allowed; a typo would silently fall into the
    'community' default at parse time, so assert on the raw JSON."""
    raw = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    for entry in raw.get("skills", []):
        tier = entry.get("trust_tier", "community")
        assert tier in ("verified", "community"), (
            f"skill {entry.get('id')!r} has unknown trust_tier {tier!r}"
        )


def test_skill_ids_are_unique():
    """`skill install <id>` resolves by id — duplicates make install
    ambiguous."""
    raw = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    ids = [e.get("id") for e in raw.get("skills", []) if e.get("id")]
    assert len(ids) == len(set(ids)), (
        f"duplicate skill ids in marketplace index: "
        f"{[i for i in ids if ids.count(i) > 1]}"
    )
