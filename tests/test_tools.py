import asyncio
import os
import pytest
from xmclaw.tools.registry import ToolRegistry
from xmclaw.tools.file_read import FileReadTool
from xmclaw.tools.file_write import FileWriteTool
from xmclaw.tools.bash import BashTool
from xmclaw.utils.paths import BASE_DIR


@pytest.mark.asyncio
async def test_tool_registry_loads():
    registry = ToolRegistry()
    await registry.load_all()
    # Registry now loads core tools + shared/skills/, so just assert core tools exist
    assert len(registry._tools) >= 8
    assert "file_read" in registry._tools
    assert "file_write" in registry._tools
    assert "bash" in registry._tools
    assert "browser" in registry._tools


@pytest.mark.asyncio
async def test_file_read_tool():
    test_file = BASE_DIR / "test_read.txt"
    test_file.write_text("hello world", encoding="utf-8")
    try:
        tool = FileReadTool()
        result = await tool.execute(file_path=str(test_file))
        assert result == "hello world"
    finally:
        if test_file.exists():
            test_file.unlink()


@pytest.mark.asyncio
async def test_file_write_tool():
    tool = FileWriteTool()
    target = BASE_DIR / "test_output.txt"
    try:
        result = await tool.execute(file_path=str(target), content="test data")
        assert "File written" in result
        assert target.read_text(encoding="utf-8") == "test data"
    finally:
        if target.exists():
            target.unlink()


@pytest.mark.asyncio
async def test_bash_tool_echo():
    tool = BashTool()
    result = await tool.execute(command="echo hello")
    assert "hello" in result
