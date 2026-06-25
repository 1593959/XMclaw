import time

from xmclaw.memory.v2.gateway_models import CognitiveDigest, Observation
from xmclaw.memory.v2.write_policy import assess_memory_write


def _obs(source: str, content: str, metadata: dict | None = None) -> Observation:
    return Observation(
        source=source,
        content=content,
        turn_id="s1",
        timestamp=time.time(),
        metadata=metadata or {},
    )


def _digest(text: str, *, kind: str = "lesson", bucket: str = "workflow") -> CognitiveDigest:
    return CognitiveDigest(
        worth_remembering=True,
        action="ADD",
        synthesized_text=text,
        kind=kind,
        scope="project",
        bucket=bucket,
        confidence=0.7,
        reason="test",
    )


def test_blocks_failed_tool_result_as_long_term_lesson() -> None:
    decision = assess_memory_write(
        _obs("tool_result", "download failed", {"tool_success": False}),
        _digest("下载失败时应该继续使用同一个镜像"),
    )

    assert decision.allow is False
    assert decision.reason == "unverified_tool_failure"


def test_blocks_unverified_post_sampling_lesson() -> None:
    decision = assess_memory_write(
        _obs("post_sampling", "assistant guessed a method"),
        _digest("下次下载微信时应该使用这个临时方法"),
    )

    assert decision.allow is False
    assert decision.reason == "unverified_extracted_lesson"


def test_allows_verified_post_sampling_lesson() -> None:
    decision = assess_memory_write(
        _obs("post_sampling", "verified", {"verified": True}),
        _digest("用户确认下载完成后应记录最终产物路径"),
    )

    assert decision.allow is True


def test_manual_memory_bypasses_policy() -> None:
    decision = assess_memory_write(
        _obs("manual", "记住：用户偏好中文"),
        _digest("用户偏好使用中文交流", kind="preference", bucket="user_preference"),
    )

    assert decision.allow is True


def test_manual_ui_memory_bypasses_policy() -> None:
    decision = assess_memory_write(
        _obs("manual_ui", "用户手动新增事实"),
        _digest("用户手动新增事实", kind="project", bucket="project_fact"),
    )

    assert decision.allow is True
