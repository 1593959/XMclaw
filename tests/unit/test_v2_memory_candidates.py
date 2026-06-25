from xmclaw.memory.v2.candidates import (
    MemoryCandidate,
    MemoryCandidateStore,
    score_candidate_quality,
)


def test_candidate_store_create_list_decide(tmp_path) -> None:
    store = MemoryCandidateStore(tmp_path / "candidates.db")
    candidate = MemoryCandidate.create(
        text="下次下载完成后必须记录最终文件路径",
        kind="lesson",
        scope="project",
        bucket="workflow",
        source="post_sampling",
        source_event_id="s1",
        confidence=0.6,
        reason="unverified_extracted_lesson",
    )

    saved = store.create(candidate)
    assert saved.id == candidate.id

    items = store.list()
    assert len(items) == 1
    assert items[0].status == "pending"
    assert items[0].reason == "unverified_extracted_lesson"

    decided = store.decide(
        candidate.id,
        status="rejected",
        reason="not enough evidence",
    )
    assert decided is not None
    assert decided.status == "rejected"
    assert decided.decision_reason == "not enough evidence"
    assert store.stats()["by_status"]["rejected"] == 1
    assert 0.0 <= items[0].quality_score <= 1.0
    assert isinstance(items[0].quality_reasons, list)


def test_candidate_store_deduplicates_same_source(tmp_path) -> None:
    store = MemoryCandidateStore(tmp_path / "candidates.db")
    one = MemoryCandidate.create(
        text="用户偏好中文",
        kind="preference",
        scope="user",
        source_event_id="s1",
    )
    two = MemoryCandidate.create(
        text="用户偏好中文",
        kind="preference",
        scope="user",
        source_event_id="s1",
    )

    assert store.create(one).id == store.create(two).id
    assert len(store.list()) == 1


def test_candidate_quality_penalizes_speculative_failed_text() -> None:
    score, reasons = score_candidate_quality(
        "可能是这个方法失败了，正在尝试，未验证",
        confidence=0.4,
        evidence=[],
        source="assistant_response",
        reason="unverified_extracted_lesson",
    )
    assert score < 0.5
    assert "speculative_or_unverified" in reasons
    assert "no_evidence" in reasons


def test_candidate_governance_rejects_low_quality_and_duplicates(tmp_path) -> None:
    store = MemoryCandidateStore(tmp_path / "candidates.db")
    weak = store.create(MemoryCandidate.create(
        text="maybe",
        confidence=0.2,
        reason="tool_failed",
        source="tool_result",
    ))
    first = store.create(MemoryCandidate.create(
        text="用户明确要求默认使用中文回答。",
        kind="preference",
        scope="user",
        bucket="user_preference",
        confidence=0.9,
        source="memory_decision",
        evidence=[{"source": "user"}],
    ))
    duplicate = store.create(MemoryCandidate.create(
        text="用户明确要求默认使用中文回答。",
        kind="preference",
        scope="user",
        bucket="user_preference",
        confidence=0.9,
        source="memory_decision",
        evidence=[{"source": "user"}],
        source_event_id="later",
    ))

    report = store.govern_pending(auto_reject_below=0.4)

    assert report["checked"] == 3
    rejected_ids = {item["id"] for item in report["rejected"]}
    assert weak.id in rejected_ids
    assert duplicate.id in rejected_ids
    assert store.get(first.id).status == "pending"
    assert store.get(weak.id).status == "rejected"
