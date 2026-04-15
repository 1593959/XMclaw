import asyncio
import pytest
from xmclaw.memory.manager import MemoryManager
from xmclaw.memory.session_manager import SessionManager


@pytest.mark.asyncio
async def test_session_manager_append_and_get(tmp_path):
    sm = SessionManager(tmp_path)
    await sm.append("agent1", {"user": "hi", "assistant": "hello"})
    await sm.append("agent1", {"user": "bye", "assistant": "goodbye"})
    recent = await sm.get_recent("agent1", limit=2)
    assert len(recent) == 2
    assert recent[0]["user"] == "hi"
    assert recent[1]["user"] == "bye"


@pytest.mark.asyncio
async def test_memory_manager_lifecycle():
    mm = MemoryManager()
    await mm.initialize()
    assert mm._initialized is True
    await mm.close()
    assert mm._initialized is False
