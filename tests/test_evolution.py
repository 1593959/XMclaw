import pytest
from xmclaw.evolution.engine import EvolutionEngine
from xmclaw.evolution.vfm import VFMScorer


class TestInsightExtraction:
    """Tests for _extract_insights()."""

    def test_tool_pattern_at_threshold_2(self):
        """Threshold >= 2 should fire for tools used 2+ times."""
        engine = EvolutionEngine("default")
        sessions = [
            {"user": "a", "tool_calls": [{"name": "bash"}]},
            {"user": "b", "tool_calls": [{"name": "bash"}]},
        ]
        insights = engine._extract_insights(sessions)
        tool_insights = [i for i in insights if i["type"] == "pattern" and "bash" in i["title"]]
        assert len(tool_insights) == 1, f"Expected bash pattern insight, got: {insights}"

    def test_tool_pattern_below_threshold_1(self):
        """Single-use tools should NOT generate an insight."""
        engine = EvolutionEngine("default")
        sessions = [
            {"user": "a", "tool_calls": [{"name": "bash"}]},
        ]
        insights = engine._extract_insights(sessions)
        tool_insights = [i for i in insights if i["type"] == "pattern" and "bash" in i["title"]]
        assert len(tool_insights) == 0

    def test_repeated_user_message(self):
        """Repeated messages >= 2 times should generate an insight."""
        engine = EvolutionEngine("default")
        sessions = [
            {"user": "fix the bug", "tool_calls": []},
            {"user": "fix the bug", "tool_calls": []},
        ]
        insights = engine._extract_insights(sessions)
        repeat_insights = [i for i in insights if i["source"] == "repeated_request"]
        assert len(repeat_insights) == 1
        assert "fix the bug" in repeat_insights[0]["description"]

    def test_distinct_repeated_messages_each_get_insight(self):
        """Different repeated messages should each get their own insight."""
        engine = EvolutionEngine("default")
        sessions = [
            {"user": "fix the bug", "tool_calls": []},
            {"user": "fix the bug", "tool_calls": []},
            {"user": "check memory", "tool_calls": []},
            {"user": "check memory", "tool_calls": []},
        ]
        insights = engine._extract_insights(sessions)
        repeat_insights = [i for i in insights if i["source"] == "repeated_request"]
        # Should have 2: one for "fix the bug", one for "check memory"
        assert len(repeat_insights) == 2

    def test_negative_feedback_problem(self):
        """Problem keyword triggers should generate a problem insight."""
        engine = EvolutionEngine("default")
        sessions = [
            {"user": "This is wrong", "tool_calls": []},
            {"user": "fix the error", "tool_calls": []},
        ]
        insights = engine._extract_insights(sessions)
        problem_insights = [i for i in insights if i["type"] == "problem"]
        assert len(problem_insights) == 2, f"Expected 2 problems, got: {insights}"
        assert all(i["source"] == "negative_feedback_user" for i in problem_insights)

    def test_distinct_problems_each_get_insight(self):
        """Different problem messages should each get their own insight."""
        engine = EvolutionEngine("default")
        sessions = [
            {"user": "wrong answer", "tool_calls": []},
            {"user": "broken feature", "tool_calls": []},
        ]
        insights = engine._extract_insights(sessions)
        problem_insights = [i for i in insights if i["type"] == "problem"]
        assert len(problem_insights) == 2

    def test_mixed_insights_all_included(self):
        """Both tool patterns and problem insights should coexist."""
        engine = EvolutionEngine("default")
        sessions = [
            {"user": "run bash", "tool_calls": [{"name": "bash"}]},
            {"user": "run bash", "tool_calls": [{"name": "bash"}]},
            {"user": "fix the bug", "tool_calls": []},
        ]
        insights = engine._extract_insights(sessions)
        assert any(i["type"] == "pattern" for i in insights)
        assert any(i["type"] == "problem" for i in insights)

    def test_empty_sessions_returns_empty(self):
        """No sessions = no insights."""
        engine = EvolutionEngine("default")
        insights = engine._extract_insights([])
        assert insights == []

    def test_novelty_keyword_in_description(self):
        """Descriptions with solid behavior keywords score higher."""
        scorer = VFMScorer()
        artifact = {
            "description": "Automatically retry failed requests with exponential backoff and circuit breaker",
            "name": "retry_circuit_breaker",
            "trigger": "when api call fails",
        }
        score = scorer._score_novelty(artifact)
        assert score >= 7.0, f"Expected high novelty score, got {score}"

    def test_genericity_penalised(self):
        """Generic/vague descriptions score low on novelty."""
        scorer = VFMScorer()
        artifact = {
            "description": "handle process manage stuff",
            "name": "auto_abc123",
            "trigger": "task",
        }
        score = scorer._score_novelty(artifact)
        assert score < 5.0, f"Expected low novelty for generic desc, got {score}"

    def test_vfm_should_solidify_default_threshold(self):
        """should_solidify returns True when total >= 5.0."""
        scorer = VFMScorer()
        scores = {"novelty": 5.0, "clarity": 5.0, "actionability": 5.0, "relevance": 5.0, "total": 5.0}
        assert scorer.should_solidify(scores, threshold=5.0) is True
        scores_below = {"total": 4.9}
        assert scorer.should_solidify(scores_below, threshold=5.0) is False

    def test_vfm_gene_score_dimensions(self):
        """score_gene returns all 4 dimensions + total."""
        scorer = VFMScorer()
        gene = {
            "name": "retry_on_error",
            "description": "Retry failed bash commands up to 3 times with backoff",
            "trigger": "when bash command returns non-zero exit code",
            "action": "execute retry logic",
            "source": "negative_feedback",
        }
        scores = scorer.score_gene(gene)
        for dim in ("novelty", "clarity", "actionability", "relevance", "total"):
            assert dim in scores, f"Missing dimension: {dim}"
        assert 0 <= scores["total"] <= 10

    def test_vfm_skill_score_dimensions(self):
        """score_skill returns all 4 dimensions + total."""
        scorer = VFMScorer()
        skill = {
            "name": "create_backup",
            "description": "Create a compressed backup of the project directory",
            "parameters": {"path": {"type": "string"}},
            "source": "repeated_request",
        }
        scores = scorer.score_skill(skill)
        for dim in ("novelty", "clarity", "actionability", "relevance", "total"):
            assert dim in scores, f"Missing dimension: {dim}"
        assert 0 <= scores["total"] <= 10

    def test_vfm_file_score(self):
        """score_file handles Python source with AST parsing."""
        import tempfile, os
        scorer = VFMScorer()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write("""
import json
def execute(path):
    with open(path) as f:
        return f.read()
def validate(result):
    return bool(result)
""")
            f.flush()
            tmp = f.name
        try:
            from pathlib import Path
            scores = scorer.score_file(Path(tmp))
            assert "total" in scores
            assert scores["total"] > 0
        finally:
            os.unlink(tmp)
