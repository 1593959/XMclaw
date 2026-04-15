import asyncio
import pytest
from xmclaw.tools.registry import ToolRegistry
from xmclaw.tools.file_read import FileReadTool
from xmclaw.tools.file_write import FileWriteTool
from xmclaw.tools.bash import BashTool


@pytest.mark.asyncio
async def test_tool_registry_loads():
    registry = ToolRegistry()
    await registry.load_all()
    assert len(registry._tools) == 8
    assert "file_read" in registry._tools
    assert "bash" in registry._tools


@pytest.mark.asyncio
async def test_file_read_tool(tmp_path):
    test_file = tmp_path / "test.txt"
    test_file.write_text("hello world", encoding="utf-8")
    tool = FileReadTool()
    result = await tool.execute(file_path=str(test_file))
    assert result == "hello world"


@pytest.mark.asyncio
async def test_file_write_tool(tmp_path):
    tool = FileWriteTool()
    target = tmp_path / "output.txt"
    result = await tool.execute(file_path=str(target), content="test data")
    assert "File written" in result
    assert target.read_text(encoding="utf-8") == "test data"


@pytest.mark.asyncio
async def test_bash_tool_echo():
    tool = BashTool()
    result = await tool.execute(command="echo hello")
    assert "hello" in result
