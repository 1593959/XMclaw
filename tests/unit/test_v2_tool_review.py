from xmclaw.cognition.tool_review import ToolFailureStrategy


def test_tool_review_path_not_found_requires_memory_and_artifact_checks() -> None:
    review = ToolFailureStrategy().review(
        tool_name="bash",
        ok=False,
        error="[path_not_found] path not found: C:\\missing",
    )

    assert review.decision == "query_memory"
    assert review.should_retry_same is False
    assert "memory_decision" in review.to_event_payload()["recommended_action"]
    assert "Artifact Ledger" in review.to_event_payload()["recommended_action"]


def test_tool_review_repeated_failure_blocks_same_retry() -> None:
    review = ToolFailureStrategy(repeat_threshold=2).review(
        tool_name="bash",
        ok=False,
        error="command failed",
        recent_failures=[{"tool": "bash", "error": "command failed"}],
    )

    assert review.repeated_count == 2
    assert review.should_retry_same is False
    assert "禁止原样重试" in review.to_event_payload()["recommended_action"]
