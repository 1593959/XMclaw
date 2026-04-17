"""Tests for ToolRegistry and skill loading."""
import pytest

from xmclaw.tools.registry import ToolRegistry


class TestToolRegistryBasics:
    """Basic tool registry functionality."""

    @pytest.mark.asyncio
    async def test_load_all_registers_tools(self):
        """load_all should populate _tools dict."""
        registry = ToolRegistry()
        await registry.load_all()
        assert len(registry._tools) > 0, "Expected tools to be loaded"

    @pytest.mark.asyncio
    async def test_known_builtins_present(self):
        """Common built-in tools should be loaded."""
        registry = ToolRegistry()
        await registry.load_all()
        expected = ["file_read", "file_write", "bash", "grep", "glob", "ask_user"]
        for tool_name in expected:
            assert tool_name in registry._tools, f"Missing built-in tool: {tool_name}"

    @pytest.mark.asyncio
    async def test_execute_returns_error_for_unknown_tool(self):
        """Unknown tool should return an error string."""
        registry = ToolRegistry()
        await registry.load_all()
        result = await registry.execute("nonexistent_tool_xyz", {})
        assert "[Error" in result
        assert "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_execute_bash(self):
        """Bash tool should execute and return output."""
        registry = ToolRegistry()
        await registry.load_all()
        result = await registry.execute("bash", {"command": "echo hello"})
        assert "hello" in result.lower()

    @pytest.mark.asyncio
    async def test_get_descriptions_returns_string(self):
        """get_descriptions should return a non-empty string."""
        registry = ToolRegistry()
        await registry.load_all()
        desc = registry.get_descriptions()
        assert isinstance(desc, str)
        assert len(desc) > 0
        assert "file_read" in desc


class TestSharedRegistry:
    """Tests for ToolRegistry shared singleton."""

    def test_set_and_get_shared(self):
        """set_shared / get_shared should work as a pair."""
        registry = ToolRegistry()
        ToolRegistry.set_shared(registry)
        assert ToolRegistry.get_shared() is registry

    def test_get_shared_none_when_not_set(self):
        """get_shared returns None before set_shared is called."""
        # Create a fresh registry instance (doesn't affect the class variable)
        fresh = ToolRegistry()
        # Clear shared state by setting to None (use reflection to reset)
        ToolRegistry._shared = None
        assert ToolRegistry.get_shared() is None
        ToolRegistry.set_shared(fresh)
        assert ToolRegistry.get_shared() is fresh


class TestHotReload:
    """Tests for hot_reload functionality."""

    @pytest.mark.asyncio
    async def test_reload_all_returns_count(self):
        """reload_all should return the tool count."""
        registry = ToolRegistry()
        await registry.load_all()
        count = await registry.reload_all()
        assert isinstance(count, int)
        assert count > 0

    @pytest.mark.asyncio
    async def test_reload_nonexistent_returns_false(self):
        """hot_reload for a non-existent tool should return False."""
        registry = ToolRegistry()
        await registry.load_all()
        result = await registry.hot_reload("totally_fake_tool_xyz123")
        assert result is False
