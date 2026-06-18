"""Epic #27 sweep #8: LanceDB connection sharing tests.

Verifies that LanceDBVectorBackend and LanceDBGraphBackend on the same
db_path share a single AsyncConnection via _LanceDBConnectionManager.
"""
from __future__ import annotations

import pytest

lancedb = pytest.importorskip("lancedb")

from unittest.mock import patch
from xmclaw.memory.v2.backend_lancedb import (
    LanceDBGraphBackend,
    LanceDBVectorBackend,
    _LanceDBConnectionManager,
)


@pytest.fixture(autouse=True)
def _reset_manager():
    """Clean connection cache before every test."""
    _LanceDBConnectionManager.reset()
    yield


@pytest.mark.asyncio
async def test_vec_and_graph_share_same_connection(tmp_path) -> None:
    """Two backends on the same db_path must share the identical connection object."""
    vb = LanceDBVectorBackend(str(tmp_path), embedding_dim=4)
    gb = LanceDBGraphBackend(str(tmp_path))
    await vb._ensure_ready()
    await gb._ensure_ready()
    assert vb._db is not None
    assert vb._db is gb._db


@pytest.mark.asyncio
async def test_connect_async_called_once_per_path(tmp_path) -> None:
    """lancedb.connect_async must be invoked exactly once per db_path."""
    calls: list[str] = []
    original = lancedb.connect_async

    async def _traced(path: str):
        calls.append(path)
        return await original(path)

    with patch.object(lancedb, "connect_async", side_effect=_traced):
        vb = LanceDBVectorBackend(str(tmp_path), embedding_dim=4)
        gb = LanceDBGraphBackend(str(tmp_path))
        await vb._ensure_ready()
        await gb._ensure_ready()
        # idempotent: second call on same backend should not re-connect
        await vb._ensure_ready()

    assert calls.count(str(tmp_path)) == 1
