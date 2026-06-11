"""History compression mixin for AgentLoop.

Extracted from agent_loop.py to reduce module size. Contains
history persistence, context compression, and LLM-based summary
upgrade logic.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from xmclaw.daemon.turn_context import _sanitize_memory_context
from xmclaw.daemon.history_utils import _estimate_history_tokens
from xmclaw.providers.llm.base import Message


class HistoryCompressionMixin:
    """Provides history compression and persistence methods.

    Designed to be mixed into AgentLoop. All methods access
    self._histories, self._session_store, self._llm,
    and other AgentLoop attributes.
    """

    async def _summarize_for_compressor(
        self, prompt: str, max_tokens: int,
    ) -> str | None:
        """Compressor's summarise_call adapter — wraps ``self._llm.complete``.

        Wall-clock-bounded so a stuck summary call doesn't add latency
        on top of an already-pressured turn. Returns None on any error
        so the compressor falls back to the static "summary unavailable"
        notice rather than failing the user turn.
        """
        try:
            msgs = [Message(role="user", content=prompt)]
            resp = await asyncio.wait_for(
                self._llm.complete(msgs, tools=None),
                timeout=60.0,
            )
            content = (resp.content or "").strip()
            return content or None
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — never fail a turn over summary
            return None

    def _resolve_context_length(self) -> int:
        """Decide the model's context window size for the compressor.

        Priority (highest first):
          1. ``self._llm.context_length`` — explicit override from the
             LLM constructor (driven by ``llm.<provider>.context_length``
             in daemon/config.json). User-facing escape hatch: works
             for ANY model, no hardcoded table needed.
          2. Static per-model lookup via
             ``get_model_context_length(model, provider_id)`` —
             default for well-known models (Claude / Kimi / Qwen /
             GPT / Gemini / DeepSeek).
          3. Dynamic ratchet: if any prior LLM call reported
             ``usage.prompt_tokens > current_estimate``, raise the
             estimate so the next compressor build matches reality.
             This is the "self-healing" path for models the static
             table doesn't know about — after one heavy call, the
             system has correct ctx_len without anyone editing code.

        The dynamic ratchet stores its high-water mark on
        ``self._observed_prompt_tokens_high_water`` (set elsewhere
        on the agent), so it survives across compressor rebuilds.
        """
        from xmclaw.providers.llm._provider_profiles import (
            get_model_context_length,
        )
        model = getattr(self._llm, "model", "") or "unknown"

        # 1. Explicit override on the LLM instance.
        override = getattr(self._llm, "context_length", None)
        if isinstance(override, int) and override > 0:
            return override

        # 2. Static lookup for known models.
        provider_id = getattr(self._llm, "provider_id", None)
        static = get_model_context_length(model, provider_id=provider_id)

        # 3. Dynamic ratchet — never SHRINK below static, but if we've
        #    seen a successful completion with more prompt_tokens than
        #    static says fits, the real window must be at least that
        #    big (with some breathing room — we divide by 0.85 since
        #    that's the threshold the provider accepted UNDER).
        high = int(getattr(self, "_observed_prompt_tokens_high_water", 0) or 0)
        if high > 0:
            implied_min = int(high / 0.85)
            return max(static, implied_min)
        return static

    def _get_compressor(self):
        """Lazy-init the ContextCompressor on first use.

        Tuning history:

        * Wave 26 fix-4 (2026-05-14): threshold 0.50 → 0.85 + per-model
          ctx_len lookup.

        * Wave 27 fix-5 (2026-05-15): user complaint "上下文压缩的还是
          太快了" — bumped protect_last_n 20 → 40,
          summary_target_ratio 0.20 → 0.30.

        * Wave 27 fix-6 (2026-05-15): user pushed back —
          "压缩不是看多少条上下文！而是看模型上下文极限" + "你注册
          minimax干嘛，那其他模型呢". Two architectural fixes:

            (a) Replaced raw ``protect_last_n=40`` (message count) with
                ``protect_last_ratio=0.20`` (fraction of ctx_len).
                Tail protection now scales with the model — a 1M-token
                window keeps ~150K verbatim, a 32K window keeps ~6.4K.
                No more "20 messages" hardcode that's right for one
                model size and wrong for every other.

            (b) Dynamic ctx_len discovery + user-facing override via
                ``llm.<provider>.context_length``. Static table stays
                for known models; observed ``prompt_tokens`` from
                successful calls ratchets UP for unknown ones. No more
                "every new model needs a code edit". See
                ``_resolve_context_length``.

        Tunables (current defaults shown):
          * context window: dynamic — see _resolve_context_length
          * 85% threshold (compress when prompt > 0.85 × ctx_len)
          * 3 head messages protected
          * 20% of ctx_len reserved as recent verbatim history
            (with 5-message integer floor)
          * 30% of threshold reserved for summary output
        """
        if self._compressor is not None:
            return self._compressor
        from xmclaw.context.compressor import ContextCompressor
        model = getattr(self._llm, "model", "") or "unknown"
        ctx_len = self._resolve_context_length()

        # Wave 27 fix-5/6: user overrides via daemon config.
        cfg = getattr(self, "_cfg", None) or {}
        sub = (
            ((cfg.get("cognition") or {}).get("context_compression") or {})
            if isinstance(cfg, dict) else {}
        )

        def _f(key: str, default: float, lo: float, hi: float) -> float:
            try:
                v = float(sub.get(key, default))
            except (TypeError, ValueError):
                return default
            return v if lo <= v <= hi else default

        def _i(key: str, default: int, lo: int, hi: int) -> int:
            try:
                v = int(sub.get(key, default))
            except (TypeError, ValueError):
                return default
            return v if lo <= v <= hi else default

        threshold_percent = _f("threshold_percent", 0.85, 0.30, 0.95)
        protect_first_n = _i("protect_first_n", 3, 0, 20)
        protect_last_n = _i("protect_last_n", 5, 1, 100)
        protect_last_ratio = _f("protect_last_ratio", 0.20, 0.05, 0.50)
        summary_target_ratio = _f("summary_target_ratio", 0.30, 0.10, 0.60)

        self._compressor = ContextCompressor(
            model=model,
            summarize_call=self._summarize_for_compressor,
            threshold_percent=threshold_percent,
            protect_first_n=protect_first_n,
            protect_last_n=protect_last_n,
            protect_last_ratio=protect_last_ratio,
            summary_target_ratio=summary_target_ratio,
            context_length=ctx_len,
            quiet_mode=False,
        )
        return self._compressor

    async def _maybe_compress_messages(
        self,
        messages: list[Message],
        session_id: str,
        *,
        force: bool = False,
    ) -> tuple[list[Message], bool]:
        """Run the compressor when threshold breached (or force=True).

        Returns ``(messages, did_compress)``. The ``did_compress`` flag
        lets the caller emit a ``CONTEXT_COMPRESSED`` event for the
        Trace UI without re-checking. Compressor errors are swallowed —
        original messages returned unchanged. Context compression
        NEVER fails a user turn.
        """
        try:
            from xmclaw.context.compressor import (
                estimate_messages_tokens_rough,
            )
            cc = self._get_compressor()
            est = estimate_messages_tokens_rough(messages)
            if force or cc.should_compress(est, session_id=session_id):
                # B-233: pass force through so anti-thrashing's
                # ineffective_count counter doesn't tick during recovery
                # compactions (the reactive path runs AFTER the provider
                # rejected the payload — by definition we have to try
                # SOMETHING; getting to "<10% savings → permanent skip"
                # turns the brake into a guaranteed crash on the next
                # token-budget breach).
                #
                # Wave 26 fix-4: pass an on_drop callback that
                # publishes CONTEXT_COMPRESSION_PENDING with the
                # doomed slice so memory subsystems can extract facts
                # BEFORE the content collapses to a one-paragraph
                # summary.
                trigger_label = "reactive" if force else "proactive"

                async def _on_drop(dropped: list[Message]) -> None:
                    await self._publish_compression_pending(
                        session_id=session_id,
                        dropped=dropped,
                        trigger=trigger_label,
                    )

                new_msgs = await cc.compress(
                    messages,
                    session_id=session_id,
                    current_tokens=est,
                    force=force,
                    on_drop=_on_drop,
                )
                return new_msgs, len(new_msgs) != len(messages)
        except Exception as exc:  # noqa: BLE001
            try:
                from xmclaw.utils.log import get_logger
                get_logger(__name__).warning(
                    "agent_loop.compress_failed session=%s err=%s",
                    session_id, exc,
                )
            except Exception:  # noqa: BLE001
                pass
        return messages, False

    async def _publish_compression_pending(
        self,
        *,
        session_id: str,
        dropped: list[Message],
        trigger: str,
    ) -> None:
        """Wave 26 fix-4: emit CONTEXT_COMPRESSION_PENDING with the
        doomed slice so memory subsystems can extract facts before the
        content collapses into a summary.

        Best-effort — never fails the compression call. Serialises
        each message into a plain dict (role/content/ts) because raw
        ``Message`` dataclass instances don't survive JSON
        round-tripping through the event bus.
        """
        bus = getattr(self, "_bus", None)
        if bus is None:
            return
        try:
            from xmclaw.core.bus.events import EventType, make_event
            from xmclaw.context.compressor import estimate_messages_tokens_rough
            serialised: list[dict[str, Any]] = []
            for m in dropped:
                content = m.content if isinstance(m.content, str) else ""
                if not content:
                    # tool_call messages carry structured tool_calls,
                    # not text. Serialise a short marker so the
                    # subscriber knows this slot was a tool invocation.
                    tcs = m.tool_calls or ()
                    if tcs:
                        names = ", ".join(
                            getattr(tc, "name", "?") for tc in tcs
                        )
                        content = f"[tool_call: {names}]"
                serialised.append({
                    "role": m.role or "?",
                    "content": content,
                })
            event = make_event(
                session_id=session_id,
                agent_id=getattr(self, "_agent_id", "main"),
                type=EventType.CONTEXT_COMPRESSION_PENDING,
                payload={
                    "session_id": session_id,
                    "dropped_messages": serialised,
                    "trigger": trigger,
                    "estimated_tokens": estimate_messages_tokens_rough(dropped),
                },
            )
            await bus.publish(event)
        except Exception:  # noqa: BLE001 — never fail compression over telemetry
            pass

    def _build_compression_summary(
        self, session_id: str, dropped: list[Message],
    ) -> str:
        """Compress a prefix of dropped history into a one-paragraph
        summary that survives as a single system message.

        B-30 deferred-LLM design:
          * THIS call (sync, inside _persist_history) always returns
            the rule-based digest — fast, safe, deterministic.
          * If LLM compression is enabled, we ALSO record the dropped
            messages on ``self._pending_llm_compression[session_id]``
            so the next ``run_turn`` can do an async LLM upgrade BEFORE
            the LLM sees the system prompt.

        This eliminates the sync→async bridge risk (which was the
        whole reason the LLM path defaulted off in B-29). The agent's
        very next turn gets the better summary; this turn's reply is
        unaffected.
        """
        if not dropped:
            return ""

        # Collect provider-extracted insights via on_pre_compress
        # regardless of compressor mode — both branches use it.
        provider_extract = ""
        try:
            mgr = self._memory_manager
            if mgr is not None and hasattr(mgr, "on_pre_compress"):
                history_dicts = [
                    {"role": m.role,
                     "content": m.content if isinstance(m.content, str) else ""}
                    for m in dropped
                ]
                provider_extract = mgr.on_pre_compress(history_dicts) or ""
        except Exception:  # noqa: BLE001
            provider_extract = ""

        # Schedule LLM compression for the next turn if enabled.
        if self._llm_compressor_enabled():
            try:
                self._pending_llm_compression[session_id] = {
                    "dropped": list(dropped),  # immutable snapshot
                    "provider_extract": provider_extract,
                    "ts": time.time(),
                }
            except Exception:  # noqa: BLE001
                pass

        # Always return rule-based digest synchronously — covers the
        # case where LLM is off, this is the FIRST overflow, or the
        # async path failed.
        return self._build_compression_summary_rule_based(
            dropped, provider_extract,
        )

    def _llm_compressor_enabled(self) -> bool:
        """True iff config opts into LLM-based compression. Default
        TRUE in B-30 (was opt-in/false in B-29) because the deferred
        async path is now safe."""
        if self._llm is None:
            return False
        try:
            from xmclaw.daemon import app as _app_mod
            state = getattr(_app_mod, "_LAST_APP_STATE", None)
            cfg = getattr(state, "config", None) if state else None
            llm_cfg = ((cfg or {}).get("llm") or {}).get("compressor") or {}
            return bool(llm_cfg.get("enabled", True))
        except Exception:  # noqa: BLE001
            return True

    async def _maybe_apply_llm_compression(self, session_id: str) -> None:
        """Pre-turn hook: if a previous turn scheduled LLM compression
        for this session, run it NOW (async-safe) and replace the
        stale rule-based summary system message with the LLM gist.

        Called from ``run_turn`` right after history is loaded but
        before the system prompt is built. Best-effort: any failure
        falls through silently and the rule-based summary stays.
        """
        pending = self._pending_llm_compression.pop(session_id, None)
        if not pending:
            return
        if not self._llm_compressor_enabled():
            return
        try:
            llm_summary = await self._compress_via_llm_async(
                pending["dropped"], pending["provider_extract"],
            )
        except Exception:  # noqa: BLE001
            return
        if not llm_summary:
            return

        # Find the rule-based summary at the start of history (always
        # the first system message inserted by _persist_history when
        # compression fired) and replace its content.
        history = self._histories.get(session_id, [])
        if not history:
            return
        head = history[0]
        if head.role != "system":
            return
        if "Earlier conversation summary" not in (head.content or ""):
            return
        import dataclasses as _dc
        history[0] = _dc.replace(head, content=llm_summary)
        self._histories[session_id] = history
        # Persist the upgrade so future loads from disk see it too.
        # 2026-05-24 user-report fix: drive the sync sqlite write off
        # the event loop so it doesn't block other in-flight turns
        # (especially WorkerSwarm fanout publishing many events/sec).
        if self._session_store is not None:
            try:
                import asyncio as _asyncio
                await _asyncio.to_thread(
                    self._session_store.save, session_id, history,
                )
            except Exception:  # noqa: BLE001
                pass

    async def _compress_via_llm_async(
        self, dropped: list[Message], provider_extract: str,
    ) -> str:
        """Run an auxiliary LLM call to produce a gist summary.

        B-30 async-only — called from run_turn (already async). No
        sync-bridging tricks needed. Returns "" if LLM unavailable
        or the call fails."""
        if self._llm is None:
            return ""

        # Build a compact transcript for the summariser.
        transcript_lines: list[str] = []
        for m in dropped[-60:]:  # cap input — first few of a 100-msg
                                   # tail rarely matter; we want recent
            if not isinstance(m.content, str) or not m.content:
                continue
            role = m.role.upper() if m.role else "?"
            line = f"[{role}] {m.content.strip()[:300]}"
            transcript_lines.append(line)

        if not transcript_lines:
            return ""

        transcript = "\n".join(transcript_lines)
        provider_block = (
            f"\n\n**Memory-layer extracted facts**:\n{provider_extract}"
            if provider_extract else ""
        )

        sys_prompt = (
            "You are a conversation compressor. Your job is to produce "
            "a tight markdown summary of an earlier conversation slice "
            "so the next turn can continue seamlessly without seeing "
            "the full transcript.\n\n"
            "Rules:\n"
            "  - Output ONLY the summary, no preamble\n"
            "  - Preserve: user identity / role, project names, "
            "decisions made, files touched, open questions, errors hit\n"
            "  - Drop: chitchat, greetings, repeated content\n"
            "  - 200 words max\n"
            "  - Use bullet points for facts; one paragraph for narrative\n"
        )
        user_prompt = (
            f"Compress this conversation slice (oldest at top, "
            f"most recent at bottom):\n\n```\n{transcript}\n```"
            f"{provider_block}\n\nReturn the summary:"
        )

        # B-30: simple async call — we're already in an async context.
        messages = [
            Message(role="system", content=sys_prompt),
            Message(role="user", content=user_prompt),
        ]
        try:
            import asyncio as _asyncio
            resp = await _asyncio.wait_for(
                self._llm.complete(messages, tools=None),
                timeout=20.0,
            )
        except (Exception, _asyncio.TimeoutError):  # noqa: BLE001
            return ""
        return (resp.content or "").strip()

    def _build_compression_summary_rule_based(
        self, dropped: list[Message], provider_extract: str,
    ) -> str:
        """Deterministic digest fallback — subgoal-aware as of 2026-05-12.

        Old behaviour: pure roles-count digest (B-28).
        New behaviour (Batch A.2 HierarchicalContextWindow): group the
        dropped slice by **user turn** (each user message starts a
        subgoal), compress each completed subgoal into a single bullet
        with its tool-call summary, keep the most recent subgoal
        intact at the tail.

        Why: Kimi K2.6's "200-300 step tool chains without drift" is
        partly inference-infra (long ctx) but partly **goal-aware
        compression** — completed sub-tasks shrink to one line, the
        live sub-task keeps full detail. Replicating that here lets a
        weak/short-context model (Qwen 7B, Llama 8B) hold up to
        long-horizon tasks without re-asking for the same things.

        Output format::

            ## Earlier conversation summary
            _Compressed N earlier messages from this session into M subgoals_:

            ### Subgoal 1: "what user said" → done
            - 5 tool calls, 4 succeeded, 1 failed
            - Key result: ...

            ### Subgoal 2: ...
            ...

            ### Most-recent subgoal (still in progress)
            (kept verbatim — see live messages above)

        Falls back gracefully on edge cases: ``dropped=[]`` → empty
        string; only-system-messages → roles-count digest. Memory-
        manager extract still appended at the bottom regardless.
        """
        if not dropped:
            return ""

        # Group by user turn — each user message starts a new subgoal.
        subgoals = self._group_into_subgoals(dropped)

        parts: list[str] = [
            "## Earlier conversation summary",
            "",
            f"_Compressed {len(dropped)} earlier messages into "
            f"{len(subgoals)} subgoal(s)_:",
            "",
        ]

        # Show each completed subgoal as one section.
        for i, sg in enumerate(subgoals, start=1):
            user_text = sg["user_text"][:200].replace("\n", " ").strip()
            n_tools = sg["tool_count"]
            n_tools_ok = sg["tool_ok"]
            n_tools_fail = sg["tool_fail"]
            assistant_synth = sg["assistant_synthesis"][:240].replace("\n", " ").strip()

            parts.append(f"### Subgoal {i}: \"{user_text}\"")
            tool_line_bits: list[str] = []
            if n_tools:
                tool_line_bits.append(
                    f"{n_tools} tool call(s)"
                    + (f", {n_tools_ok} ok" if n_tools_ok else "")
                    + (f", {n_tools_fail} failed" if n_tools_fail else "")
                )
                if sg["tool_names"]:
                    head = ", ".join(sg["tool_names"][:6])
                    if len(sg["tool_names"]) > 6:
                        head += f", +{len(sg['tool_names']) - 6} more"
                    tool_line_bits.append(f"tools: {head}")
            if tool_line_bits:
                parts.append("- " + " · ".join(tool_line_bits))
            if assistant_synth:
                parts.append(f"- Synthesis: \"{assistant_synth}\"")
            parts.append("")

        # Roles-count footer for parity with old digest (some downstream
        # consumers grep for "user: N message(s)").
        roles: dict[str, int] = {}
        for m in dropped:
            roles[m.role] = roles.get(m.role, 0) + 1
        roles_line = ", ".join(
            f"{r}: {roles[r]}"
            for r in ("user", "assistant", "tool", "system")
            if r in roles
        )
        if roles_line:
            parts.append(f"_(Roles: {roles_line}.)_")

        if provider_extract:
            parts.append("")
            parts.append("**Memory-extracted facts to preserve:**")
            parts.append(provider_extract)
        parts.append("")
        parts.append(
            "_(Use this summary as background; recent turns above "
            "are the live context.)_"
        )
        return "\n".join(parts)

    @staticmethod
    def _group_into_subgoals(
        dropped: "list[Message]",
    ) -> list[dict[str, Any]]:
        """Split a flat message list into subgoals — one per user turn.

        Returns a list of dicts with fields:
          * ``user_text``        — the user's prompt that started this subgoal
          * ``tool_count``       — total tool calls in this subgoal
          * ``tool_ok``          — successful ones
          * ``tool_fail``        — failed ones
          * ``tool_names``       — unique tool names called (preserves order)
          * ``assistant_synthesis`` — the LAST non-empty assistant text in
            this subgoal (treated as the "answer" the LLM eventually gave)

        Tool messages contribute to whatever subgoal their preceding
        user message established. Stray system messages are attached
        to the nearest subgoal. If no user message exists, returns one
        subgoal labelled "(pre-user context)".
        """
        if not dropped:
            return []

        subgoals: list[dict[str, Any]] = []
        cur: dict[str, Any] | None = None
        seen_tools_in_cur: set[str] = set()

        def _new_subgoal(user_text: str) -> dict[str, Any]:
            return {
                "user_text": user_text,
                "tool_count": 0,
                "tool_ok": 0,
                "tool_fail": 0,
                "tool_names": [],
                "assistant_synthesis": "",
            }

        for m in dropped:
            if m.role == "user" and isinstance(m.content, str):
                # Start a new subgoal — user explicitly said something.
                cur = _new_subgoal(m.content)
                seen_tools_in_cur = set()
                subgoals.append(cur)
                continue
            if cur is None:
                cur = _new_subgoal("(pre-user context)")
                seen_tools_in_cur = set()
                subgoals.append(cur)

            # Assistant turn — track tool_calls + capture synthesis text.
            if m.role == "assistant":
                content = m.content
                tool_calls = getattr(m, "tool_calls", None) or ()
                for tc in tool_calls:
                    cur["tool_count"] += 1
                    name = getattr(tc, "name", None)
                    if isinstance(name, str) and name not in seen_tools_in_cur:
                        cur["tool_names"].append(name)
                        seen_tools_in_cur.add(name)
                # Final answer text — keep the latest non-empty.
                if isinstance(content, str) and content.strip():
                    cur["assistant_synthesis"] = content
            elif m.role == "tool":
                # tool-result message: classify ok / fail by content hint.
                content = m.content
                text = (
                    content if isinstance(content, str)
                    else (str(content) if content is not None else "")
                )
                low = text[:400].lower()
                if any(k in low for k in (
                    '"ok": false', "error:", "failed", "permission denied",
                    "not found", "timeout",
                )):
                    cur["tool_fail"] += 1
                else:
                    cur["tool_ok"] += 1

        return subgoals

    async def _persist_history(
        self, session_id: str, messages: list[Message],
    ) -> dict[str, Any] | None:
        """Save conversation history (system prompt excluded).

        Wave-27 fix-LAT (2026-05-16): the legacy ``msg_cap`` / ``token_cap``
        gates that used to live here were removed. They fired on raw
        message count (default 40) regardless of how small each message
        was — a chat with 5 tool-using turns easily hit 40 messages
        totalling 500 tokens, which is 0.2% of a 256K-window model's
        budget, and the rule-based "summary" was often LONGER than the
        content it replaced. Empirical: chat-0ac7a48b on 2026-05-16
        dropped 11 messages = 468 tokens, summary = 526 chars → negative
        compression.

        Compression now lives entirely in ``_maybe_compress_messages``
        (hop_loop.py:372, pre-LLM) which runs ``ContextCompressor`` with
        a real token estimate (CJK-aware), an 85%-of-ctx threshold, and
        a 5-phase LLM-summarised pipeline. We still do the cheap stuff
        here (system-msg drop, memory-context fence scrub, tool-result
        prune) before persisting to ``session_store``.

        Always returns ``None`` (no compression-info event from this
        path anymore). The pre-LLM path emits its own
        ``CONTEXT_COMPRESSED`` event with full details.
        """
        # Drop the system message we prepended for this turn.
        history = [m for m in messages if m.role != "system"]
        # B-25: strip memory-context fences from user messages before
        # persisting. The injected ``<memory-context>...</memory-
        # context>`` block was useful for THIS turn's LLM call — it
        # must NOT survive into history, or every subsequent turn
        # would see the prefetched recall as part of the user's
        # actual words (and the model would echo it back as if the
        # user had said it). the upstream agent does this in its memory_manager.
        import dataclasses as _dc
        cleaned_history: list[Message] = []
        # Sprint 3 #6: extend the predicate to ALSO catch
        # ``<curriculum-strategies>`` and ``<curriculum-hint>`` so
        # those blocks get scrubbed before persistence — same rationale
        # as the original memory-context scrub.
        _SCRUB_MARKERS = (
            "memory-context",
            "curriculum-hint",
            "curriculum-strategies",
            "memory-files",
            "<memory-v2-facts>",
            "【相关记忆】",
            # 2026-05-17: ``[turn hint]`` is appended to user
            # messages by agent_loop.py when no local skill matched
            # the query. Same "in-flight scaffolding leaking into
            # persisted history" failure mode as GOAL-ANCHOR; both
            # the marker-detection here AND turn_context's regex
            # stripper need to know about it.
            "[turn hint]",
            # Jarvis Phase 1-2: time_block moved system → user msg.
            "## 当前时刻",
            # Fix Bug C (audit 2026-06-11): continuation anchor leaks into
            # persisted history on "继续"/"go on" turns. Also scrub git
            # status and output schema blocks that are turn scaffolding.
            "[System note:",
            "</output_schema>",
            "git status:",
        )
        for m in history:
            # 2026-05-17: drop GOAL-ANCHOR user messages entirely from
            # the persisted history. hop_loop.py:293 injects them as a
            # per-turn scaffolding (every N hops, plus once at hop 0
            # on multi-turn sessions). They have no value in the
            # on-disk record — they're regenerated from scratch each
            # turn from the real history's user thread — and worse,
            # leaving them in causes role-sequence corruption: the
            # NEXT turn's run_turn hydrates them as part of ``prior``,
            # appends the new user msg, hop 0 injects a fresh anchor,
            # and persists [user, asst, user, anchor, asst] instead
            # of [user, asst, user, asst]. The duplicate user message
            # then forms the basis of the NEXT-next turn's anchor,
            # which compounds across the session. Surfaced by
            # tests/unit/test_session_store.py:test_agent_loop_persists_after_each_turn
            # which exercises exactly this two-turn-with-resume path.
            if (
                m.role == "user"
                and isinstance(m.content, str)
                and m.content.lstrip().startswith("[GOAL-ANCHOR]")
            ):
                continue
            if (
                m.role == "user"
                and isinstance(m.content, str)
                and any(mk in m.content for mk in _SCRUB_MARKERS)
            ):
                cleaned_history.append(_dc.replace(
                    m, content=_sanitize_memory_context(m.content),
                ))
            else:
                cleaned_history.append(m)
        history = cleaned_history

        # B-226: prune old tool results FIRST (before deciding to
        # drop turns). Most context bloat is huge tool outputs (file
        # reads, web fetches, grep results) that the model doesn't
        # need verbatim 30 turns later. Replacing them with 1-line
        # summaries often gets us back under the token cap without
        # losing any turn boundaries. Returns (new_history, count) —
        # count is logged at debug level inside the prune helper, no
        # need to expose here.
        # Wave-28: raised protect_tail_tokens from 6000 → 12000
        # (protect_tail_count_floor from 6 → 10) so older tool results
        # survive longer. 6000 tokens ≈ 6 messages; for a session with
        # heavy tool use that's too aggressive — the agent "forgets"
        # what it did 3 turns ago.
        if len(history) > 10:
            try:
                from xmclaw.context.tool_result_prune import (
                    prune_old_tool_results,
                )
                history, _ = prune_old_tool_results(
                    history,
                    protect_tail_tokens=12000,
                    protect_tail_count_floor=10,
                )
            except Exception:  # noqa: BLE001 — never fail a turn over compression
                pass

        # Wave-27 fix-LAT: no more post-turn msg_cap / token_cap gate.
        # Pre-LLM ContextCompressor (hop_loop.py:372) handles compression
        # token-aware. We just persist the cleaned history.
        self._histories[session_id] = history
        if self._session_store is not None:
            # Wave-28: await the save so we KNOW it lands on disk.
            # Fire-and-forget created a race where the user refreshes
            # before the async task runs → "session lost".
            try:
                import asyncio as _asyncio
                await _asyncio.to_thread(
                    self._session_store.save, session_id, history,
                )
            except Exception:  # noqa: BLE001
                # Persistence is best-effort -- a corrupt sessions.db should
                # never break the live turn. The in-memory copy is the source
                # of truth for the rest of this process.
                pass
        # Epic #2 Phase 2: sync to ContextEngine when wired.
        # The engine is a shadow store today; future refactor will
        # make it the primary source of truth.
        if getattr(self, "_context_engine", None) is not None:
            try:
                asyncio.create_task(
                    self._sync_engine_history(session_id, history)
                )
            except Exception:  # noqa: BLE001
                pass
        return None

    async def _sync_engine_history(
        self, session_id: str, history: list[Any],
    ) -> None:
        """Push cleaned history into the optional ContextEngine.

        Best-effort fire-and-forget: any failure is swallowed so the
        engine never breaks a turn.
        """
        engine = getattr(self, "_context_engine", None)
        if engine is None:
            return
        try:
            # Re-bootstrap (idempotent) then ingest any messages the
            # engine doesn't already know about.
            await engine.bootstrap(session_id)
            _assembled = await engine.assemble(
                session_id, token_budget=999_999, include_system=False,
            )
            known = list(_assembled.messages)
            # Ingest only new tail messages.
            for msg in history[len(known):]:
                await engine.ingest(session_id, msg)
            await engine.after_turn(session_id)
        except Exception:  # noqa: BLE001
            pass

