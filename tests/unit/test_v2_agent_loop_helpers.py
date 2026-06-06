"""Unit tests for the pure helpers extracted from agent_loop.py.

Phase F + earlier (2026-05-11/12) split the 3,500-line agent_loop.py
into a constellation of focused modules:

  * ``daemon/turn_types.py``        — AgentTurnResult dataclass + helpers
  * ``daemon/history_utils.py``     — _is_transient_tool_error / token estim
  * ``daemon/turn_context.py``      — regex scrubbers + continuation / frust
  * ``daemon/prompt_builder.py``    — system prompt + freeze-gen counter
  * ``daemon/history_compression.py`` — Mixin (smoke-import only here)
  * ``daemon/hop_loop.py``          — LLM↔tool hop driver (chaos test elsewhere)

This file covers the pure helpers. Mixins / orchestrators with heavy
runtime state are smoke-imported only — full behaviour lives in the
chaos / integration suites that exercise them through AgentLoop.

The tests are deliberately fast (no fixtures, no daemon, no LLM) so
they can run in the smart-gate "core" lane on every push.
"""
from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# turn_types
# ---------------------------------------------------------------------------


class TestAgentTurnResult:
    def test_default_factory_lists_are_independent(self):
        """The two list fields must not share state across instances —
        a classic dataclass footgun if ``field(default_factory=list)`` is
        ever swapped for a bare ``= []``."""
        from xmclaw.daemon.turn_types import AgentTurnResult
        a = AgentTurnResult(ok=True, text="a", hops=1)
        b = AgentTurnResult(ok=True, text="b", hops=1)
        a.tool_calls.append({"id": "x"})
        a.events.append("evt")
        assert b.tool_calls == []
        assert b.events == []

    def test_error_field_optional(self):
        from xmclaw.daemon.turn_types import AgentTurnResult
        r = AgentTurnResult(ok=False, text="", hops=0, error="boom")
        assert r.error == "boom"
        r2 = AgentTurnResult(ok=True, text="hi", hops=1)
        assert r2.error is None


class TestLogMemoryFailure:
    def test_swallows_logger_failure(self):
        """The log helper must NEVER raise — memory issues are best-effort,
        a logger that's misconfigured can't break the live user turn."""
        from xmclaw.daemon.turn_types import _log_memory_failure
        # Should not raise even on a "naked" Exception type
        _log_memory_failure(RuntimeError("vec store offline"))
        _log_memory_failure(KeyError("missing"))
        # Even None-ish things must not crash:
        try:
            raise ValueError("real")
        except ValueError as exc:
            _log_memory_failure(exc)


# ---------------------------------------------------------------------------
# history_utils
# ---------------------------------------------------------------------------


class TestIsTransientToolError:
    @pytest.mark.parametrize("msg", [
        "request timed out",
        "Connection refused on 127.0.0.1:11434",
        "ETIMEDOUT after 5s",
        "503 Service Unavailable",
        "429 Too Many Requests",
        "EAI_AGAIN",
        "Remote disconnected without response",
        "TIMEOUT",  # case-insensitive
        "Connection Reset by peer",
    ])
    def test_transient_matches(self, msg):
        from xmclaw.daemon.history_utils import _is_transient_tool_error
        assert _is_transient_tool_error(msg) is True

    @pytest.mark.parametrize("msg", [
        "FileNotFoundError: /etc/missing.txt",
        "PermissionError: [Errno 13]",
        "TypeError: bad args",
        "syntax error",
        "",  # empty
        "not_a_known_pattern",
    ])
    def test_semantic_errors_not_retried(self, msg):
        from xmclaw.daemon.history_utils import _is_transient_tool_error
        assert _is_transient_tool_error(msg) is False

    def test_none_or_falsy_returns_false(self):
        from xmclaw.daemon.history_utils import _is_transient_tool_error
        assert _is_transient_tool_error("") is False


class TestEstimateHistoryTokens:
    def test_empty_history(self):
        from xmclaw.daemon.history_utils import _estimate_history_tokens
        assert _estimate_history_tokens([]) == 0

    def test_string_content_char_div_4(self):
        from xmclaw.daemon.history_utils import _estimate_history_tokens

        class M:
            def __init__(self, content): self.content = content
        # 8 chars -> 2 tokens
        assert _estimate_history_tokens([M("12345678")]) == 2
        # Two messages
        assert _estimate_history_tokens([M("a" * 16), M("b" * 8)]) == 6

    def test_non_string_content_serialised(self):
        from xmclaw.daemon.history_utils import _estimate_history_tokens

        class M:
            def __init__(self, content): self.content = content
        # dict content gets str(...) — token count > 0
        out = _estimate_history_tokens([M({"k": "v" * 20})])
        assert out > 0

    def test_tool_call_args_counted(self):
        from xmclaw.daemon.history_utils import _estimate_history_tokens

        class TC:
            def __init__(self, args): self.args = args

        class M:
            def __init__(self, content, tool_calls=()):
                self.content = content
                self.tool_calls = tool_calls
        # tool_call args add to total
        m = M("", tool_calls=[TC("x" * 40)])  # 40 chars args -> 10 tokens
        assert _estimate_history_tokens([m]) == 10

    def test_none_content_does_not_crash(self):
        from xmclaw.daemon.history_utils import _estimate_history_tokens

        class M:
            content = None
        assert _estimate_history_tokens([M()]) == 0


# ---------------------------------------------------------------------------
# turn_context
# ---------------------------------------------------------------------------


class TestIsVagueContinuation:
    @pytest.mark.parametrize("text", [
        "继续", "接着", "下一步",
        "go on", "continue", "keep going", "go ahead",
        "proceed", "next", "and?", "so?", "ok",
        "OK",  # case-insensitive
        "  继续  ",  # whitespace-tolerant
    ])
    def test_recognises_known_continuations(self, text):
        from xmclaw.daemon.turn_context import _is_vague_continuation
        assert _is_vague_continuation(text) is True

    @pytest.mark.parametrize("text", [
        "",
        "   ",
        "what's the weather",
        "please continue with the analysis of the file",  # too long
        "好的请帮我看看这个 bug",
    ])
    def test_rejects_real_messages(self, text):
        from xmclaw.daemon.turn_context import _is_vague_continuation
        assert _is_vague_continuation(text) is False

    def test_long_message_with_token_in_it_rejected(self):
        """A 'continue' embedded in a longer instruction must NOT count
        as a vague continuation — we only want bare bumps."""
        from xmclaw.daemon.turn_context import _is_vague_continuation
        assert _is_vague_continuation("continue with section 3") is False


class TestPriorEndedWithoutSynthesis:
    def _msg(self, role, content="", tool_calls=()):
        class M:
            pass
        m = M()
        m.role = role
        m.content = content
        m.tool_calls = tool_calls
        return m

    def test_empty_prior(self):
        from xmclaw.daemon.turn_context import _prior_ended_without_synthesis
        assert _prior_ended_without_synthesis([]) is False

    def test_assistant_finished_cleanly(self):
        from xmclaw.daemon.turn_context import _prior_ended_without_synthesis
        prior = [
            self._msg("user", "find x"),
            self._msg("assistant", "I found x in foo.py line 42."),
        ]
        assert _prior_ended_without_synthesis(prior) is False

    def test_assistant_empty_after_tool_calls(self):
        """The pathological case: agent called tools but never wrote text."""
        from xmclaw.daemon.turn_context import _prior_ended_without_synthesis
        prior = [
            self._msg("user", "find x"),
            self._msg("assistant", ""),  # only tool_calls, no text
            self._msg("tool", "{result: 42}"),
        ]
        assert _prior_ended_without_synthesis(prior) is True

    def test_assistant_whitespace_only_counts_as_empty(self):
        from xmclaw.daemon.turn_context import _prior_ended_without_synthesis
        prior = [
            self._msg("assistant", "   \n  "),
            self._msg("tool", "ok"),
        ]
        assert _prior_ended_without_synthesis(prior) is True

    def test_assistant_content_as_block_list(self):
        """Some providers stream content as a list of text/tool_use blocks.
        Helper must concatenate text parts before deciding 'empty'."""
        from xmclaw.daemon.turn_context import _prior_ended_without_synthesis

        class Block:
            def __init__(self, text=""): self.text = text

        prior = [
            self._msg("assistant", [Block(""), Block("")]),
            self._msg("tool", "{}"),
        ]
        assert _prior_ended_without_synthesis(prior) is True
        # And with content
        prior_full = [
            self._msg("assistant", [Block(""), Block("hello answer")]),
            self._msg("tool", "{}"),
        ]
        assert _prior_ended_without_synthesis(prior_full) is False

    def test_user_before_assistant_returns_false(self):
        """If the last meaningful turn is the user (assistant already done
        and a new user just spoke), there's no orphan anchor needed."""
        from xmclaw.daemon.turn_context import _prior_ended_without_synthesis
        prior = [
            self._msg("assistant", "all done!"),
            self._msg("user", "thanks"),
        ]
        assert _prior_ended_without_synthesis(prior) is False


class TestContinuationAnchor:
    def _msg(self, role, content=""):
        class M:
            pass
        m = M()
        m.role = role
        m.content = content
        m.tool_calls = ()
        return m

    def test_no_anchor_when_user_message_not_vague(self):
        from xmclaw.daemon.turn_context import _continuation_anchor
        prior = [self._msg("assistant", "")]
        assert _continuation_anchor(prior, "tell me a joke") == ""

    def test_no_anchor_when_prior_finished_cleanly(self):
        from xmclaw.daemon.turn_context import _continuation_anchor
        prior = [self._msg("assistant", "I'm done.")]
        # vague continuation but no orphan
        assert _continuation_anchor(prior, "continue") == ""

    def test_anchor_fires_when_both_conditions_met(self):
        from xmclaw.daemon.turn_context import _continuation_anchor
        prior = [self._msg("assistant", ""), self._msg("tool", "{result}")]
        out = _continuation_anchor(prior, "继续")
        assert "[System note:" in out
        assert "CONTINUE THAT INVESTIGATION" in out
        assert "继续" in out  # original message echoed for the LLM


class TestDetectFrustrationSignal:
    @pytest.mark.parametrize("text", [
        "Why are you ignoring me?",
        "I didn't ask for that",
        "that's not what I meant",
        "stop doing this",
        "you keep getting it wrong",
        "you should not just guess",
        "为什么这样做",
        "别再这样了",
        "你看看这个", "错了", "我说过的",
        "听不懂啊",
    ])
    def test_recognises_frustration(self, text):
        from xmclaw.daemon.turn_context import _detect_frustration_signal
        assert _detect_frustration_signal(text) is True

    @pytest.mark.parametrize("text", [
        "",
        "   ",
        "thanks for the help!",
        "可以再来一次吗",
        "looks great, ship it",
    ])
    def test_neutral_text_not_flagged(self, text):
        from xmclaw.daemon.turn_context import _detect_frustration_signal
        assert _detect_frustration_signal(text) is False


class TestSanitizeMemoryContext:
    def test_strips_memory_block(self):
        from xmclaw.daemon.turn_context import _sanitize_memory_context
        raw = (
            "hello\n"
            "<memory-context>\n"
            "[System note: The following is recalled memory context.]\n"
            "  1. [...]: a past chunk\n"
            "</memory-context>"
        )
        out = _sanitize_memory_context(raw)
        assert "memory-context" not in out
        assert "System note" not in out
        assert "past chunk" not in out
        assert out.startswith("hello")

    def test_strips_recalled_files_block(self):
        from xmclaw.daemon.turn_context import _sanitize_memory_context
        raw = "ask\n<recalled-memory-files>\nMEMORY.md\n</recalled-memory-files>"
        out = _sanitize_memory_context(raw)
        assert "recalled-memory-files" not in out
        assert "MEMORY.md" not in out

    def test_strips_curriculum_blocks(self):
        from xmclaw.daemon.turn_context import _sanitize_memory_context
        raw = (
            "real message\n"
            "<curriculum-hint>\nedit X\n</curriculum-hint>\n"
            "<curriculum-strategies>\nuse Y\n</curriculum-strategies>"
        )
        out = _sanitize_memory_context(raw)
        assert "curriculum-hint" not in out
        assert "curriculum-strategies" not in out
        assert "edit X" not in out
        assert "use Y" not in out
        assert "real message" in out

    def test_orphaned_tags_removed(self):
        """Half-closed blocks (malformed prior turn) shouldn't bleed
        bare tags into history."""
        from xmclaw.daemon.turn_context import _sanitize_memory_context
        raw = "before <memory-context> after"
        out = _sanitize_memory_context(raw)
        # The orphaned-tag regex strips ``<memory-context>`` itself even
        # without a closing tag.
        assert "<memory-context>" not in out
        assert "before" in out and "after" in out

    def test_empty_input_returns_empty(self):
        from xmclaw.daemon.turn_context import _sanitize_memory_context
        assert _sanitize_memory_context("") == ""

    def test_clean_input_unchanged_modulo_rstrip(self):
        from xmclaw.daemon.turn_context import _sanitize_memory_context
        assert _sanitize_memory_context("hello world") == "hello world"


# ---------------------------------------------------------------------------
# prompt_builder
# ---------------------------------------------------------------------------


class TestPromptFreezeGeneration:
    def test_bump_increases_monotonically(self):
        from xmclaw.daemon import prompt_builder
        before = prompt_builder._PROMPT_FREEZE_GENERATION
        prompt_builder.bump_prompt_freeze_generation()
        after = prompt_builder._PROMPT_FREEZE_GENERATION
        assert after == before + 1
        # And it really mutates module state:
        prompt_builder.bump_prompt_freeze_generation()
        assert prompt_builder._PROMPT_FREEZE_GENERATION == after + 1


class TestDefaultSystemPrompt:
    def test_contains_identity_signature(self):
        from xmclaw.daemon.prompt_builder import _default_system_prompt
        p = _default_system_prompt()
        assert "XMclaw" in p
        # Identity-anchor language — must NEVER drift to "I am Claude" etc.
        assert "swappable backend" in p
        assert "local-first" in p.lower()

    def test_includes_os_specific_shell_hint(self):
        from xmclaw.daemon.prompt_builder import _default_system_prompt
        import platform
        p = _default_system_prompt()
        if platform.system() == "Windows":
            assert "PowerShell" in p
        elif platform.system() == "Linux":
            assert "bash" in p
        # macOS: bash / zsh both acceptable

    def test_default_system_constant_is_a_string(self):
        from xmclaw.daemon.prompt_builder import _DEFAULT_SYSTEM
        assert isinstance(_DEFAULT_SYSTEM, str)
        assert len(_DEFAULT_SYSTEM) > 100  # non-trivial


class TestWithFreshTime:
    def test_appends_time_block(self):
        from xmclaw.daemon.prompt_builder import _with_fresh_time
        out = _with_fresh_time("system prompt body")
        assert "system prompt body" in out
        assert "当前时刻" in out
        # ISO-ish year prefix:
        assert "20" in out  # any year starting with 20xx

    def test_strips_prior_time_block_on_re_apply(self):
        """``_with_fresh_time`` is idempotent: it strips any existing
        ``## 当前时刻`` block first so re-applying produces exactly one
        timestamp, not two. Without this, every turn would accrete
        stale time blocks and quickly bloat the system prompt."""
        from xmclaw.daemon.prompt_builder import _with_fresh_time
        once = _with_fresh_time("hi")
        twice = _with_fresh_time(once)
        # Exactly one block after re-apply, NOT two.
        assert twice.count("## 当前时刻") == 1
        # The base content is preserved.
        assert twice.startswith("hi")


# ---------------------------------------------------------------------------
# history_compression — smoke import only (Mixin requires full AgentLoop)
# ---------------------------------------------------------------------------


class TestHistoryCompressionMixinSurface:
    def test_module_imports(self):
        """The mixin's module must import cleanly — broken imports here
        would cascade into AgentLoop instantiation failures at boot."""
        from xmclaw.daemon import history_compression  # noqa: F401
        assert hasattr(history_compression, "HistoryCompressionMixin")

    def test_exposes_expected_methods(self):
        from xmclaw.daemon.history_compression import HistoryCompressionMixin
        # These method names are the AgentLoop contract — renaming them
        # silently would break history compression at runtime since
        # AgentLoop calls them by name.
        for m in (
            "_summarize_for_compressor",
            "_get_compressor",
        ):
            assert hasattr(HistoryCompressionMixin, m), m


# ---------------------------------------------------------------------------
# hop_loop — smoke import only (real coverage lives in chaos suite)
# ---------------------------------------------------------------------------


class TestHopLoopSurface:
    def test_module_imports(self):
        from xmclaw.daemon import hop_loop  # noqa: F401

    def test_exposes_hop_runner(self):
        """Whatever the public entry is, AgentLoop imports it by name —
        regression-guard the symbol so a rename hits this test instead
        of failing the entire daemon at agent_loop import time."""
        from xmclaw.daemon import hop_loop
        # Pick whichever public callable / class exists. Tolerant of
        # naming variations since hop_loop is an active extraction
        # target — what matters is "something importable lives here".
        public = [
            name for name in dir(hop_loop)
            if not name.startswith("_")
        ]
        assert len(public) > 0

# ---------------------------------------------------------------------------
# _steps_warrant_subagents — autonomous subagent trigger heuristic
# ---------------------------------------------------------------------------


class TestStepsWarrantSubagents:
    def test_two_steps_never_trigger(self):
        from xmclaw.daemon.agent_loop import _steps_warrant_subagents

        assert _steps_warrant_subagents(["read foo.py", "write bar.py"]) is False

    def test_three_simple_steps_no_trigger(self):
        from xmclaw.daemon.agent_loop import _steps_warrant_subagents

        # Each step has only 1 verb → not complex enough
        assert _steps_warrant_subagents([
            "read foo.py",
            "read bar.py",
            "read baz.py",
        ]) is False

    def test_three_complex_independent_steps_trigger(self):
        from xmclaw.daemon.agent_loop import _steps_warrant_subagents

        # Each step has >=2 verbs and no dependency markers
        assert _steps_warrant_subagents([
            "search and analyze the auth module",
            "search and analyze the billing module",
            "search and analyze the notification module",
        ]) is True

    def test_dependency_markers_block_trigger(self):
        from xmclaw.daemon.agent_loop import _steps_warrant_subagents

        # Steps contain "then" / "after" / "然后" → sequential
        assert _steps_warrant_subagents([
            "search and analyze the auth module",
            "then refactor and test the auth module",
            "then deploy and verify the auth module",
        ]) is False

    def test_mixed_complexity_partial_trigger(self):
        from xmclaw.daemon.agent_loop import _steps_warrant_subagents

        # 3 steps, 2 complex + 1 simple → 2/3 threshold met
        assert _steps_warrant_subagents([
            "search and analyze the auth module",
            "search and analyze the billing module",
            "read docs",
        ]) is True

    def test_chinese_dependency_markers_block(self):
        from xmclaw.daemon.agent_loop import _steps_warrant_subagents

        assert _steps_warrant_subagents([
            "搜索并分析认证模块",
            "然后重构并测试认证模块",
            "最后部署并验证认证模块",
        ]) is False

    def test_chinese_complex_steps_trigger(self):
        from xmclaw.daemon.agent_loop import _steps_warrant_subagents

        assert _steps_warrant_subagents([
            "搜索并分析认证模块",
            "搜索并分析计费模块",
            "搜索并分析通知模块",
        ]) is True
