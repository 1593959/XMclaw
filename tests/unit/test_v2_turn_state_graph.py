from xmclaw.daemon.turn_state_graph import TurnStateGraph


def test_turn_state_graph_tracks_phase_lifecycle() -> None:
    graph = TurnStateGraph.create(
        session_id="s1",
        run_id="r1",
        user_message="处理任务",
    )

    graph.start("recall", query="处理任务")
    graph.complete("recall", hits=2)
    graph.start("skill_discovery")
    graph.fail("skill_discovery", "catalog unavailable")
    graph.finalize("failed")

    snap = graph.state.snapshot()
    phases = {item["id"]: item for item in snap["subtasks"]}
    assert phases["recall"]["status"] == "completed"
    assert phases["recall"]["metadata"]["hits"] == 2
    assert phases["skill_discovery"]["status"] == "failed"
    assert snap["errors"][0]["node_id"] == "skill_discovery"
    assert snap["final"] == "failed"
