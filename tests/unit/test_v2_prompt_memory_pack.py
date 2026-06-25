from xmclaw.daemon.prompt_memory_pack import PromptMemoryPack


def test_prompt_memory_pack_orders_sections_and_preserves_content() -> None:
    pack = PromptMemoryPack()
    pack.add("late", "<late>z</late>", source="test", priority=20)
    pack.add("empty", "   ", source="test", priority=1)
    pack.add("early", "<early>a</early>", source="test", priority=10)

    rendered = pack.render()

    assert rendered.startswith("<prompt-memory-pack>")
    assert "<section name=\"early\" source=\"test\">" in rendered
    assert "<early>a</early>" in rendered
    assert "empty" not in rendered
    assert rendered.index("early") < rendered.index("late")


def test_prompt_memory_pack_empty_returns_empty_string() -> None:
    assert PromptMemoryPack().render() == ""
