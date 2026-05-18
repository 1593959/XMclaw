"""Built-in flag catalogue.

The flag NAME is canonical — call sites reference it as a string
(``default_engine().is_enabled("cognition.idle_aware")``). When a
flag goes general-availability, delete it from this catalogue and
inline the on-state.

Adding a flag here is the only way for ``snapshot()`` to surface
it in the operator UI. Unregistered flags still resolve (the
caller's default applies) but they're invisible.
"""
from __future__ import annotations

from xmclaw.core.feature_flags.flags import FeatureFlag


BUILTIN_FLAGS: list[FeatureFlag] = [
    # ── Hook engine (Wave-32) ────────────────────────────────────
    FeatureFlag(
        name="hooks.enabled",
        default=True,
        description=(
            "Master switch for the Wave-32 hook engine. When false,"
            " agent_loop/hop_loop skip every lifecycle dispatch even"
            " if config.hooks is non-empty. Useful for incident"
            " response (a buggy user hook is making the daemon"
            " spammy)."
        ),
    ),
    FeatureFlag(
        name="hooks.command_runner.enabled",
        default=True,
        description=(
            "Allow the ``command`` runner kind. Disable to block all"
            " shell-execution hooks on a workspace without revoking"
            " trust."
        ),
    ),
    FeatureFlag(
        name="hooks.function_runner.enabled",
        default=True,
        description=(
            "Allow the ``function`` runner kind (importing arbitrary"
            " Python). Same RCE surface as command — separate switch"
            " in case you want one but not the other."
        ),
    ),

    # ── Memory pipeline ──────────────────────────────────────────
    FeatureFlag(
        name="memory.v2.dual_write",
        default=True,
        description=(
            "When true, ExtractFactsHook also writes facts to the v2"
            " LanceDB pipeline (in addition to persona MD files)."
            " Turn off if v2 is misbehaving and you want to fall back"
            " to MD-only writes."
        ),
    ),
    FeatureFlag(
        name="memory.recall.hybrid",
        default=True,
        description=(
            "Use RRF-merged vector+keyword recall (B-50) for cross-"
            "session memory. False = vector-only path."
        ),
    ),

    # ── Cache (Wave-30/Wave-32) ───────────────────────────────────
    FeatureFlag(
        name="prompt_cache.history_breakpoint",
        default=True,
        description=(
            "Tag the last message with cache_control so the prior-"
            "history prefix is cached (Wave-30 follow-up). Disable"
            " if your provider rejects the marker or you need to"
            " A/B-test cache hit rate against the pre-Wave-30"
            " baseline."
        ),
    ),

    # ── Cognition ────────────────────────────────────────────────
    FeatureFlag(
        name="cognition.idle_aware_scheduling",
        default=True,
        description=(
            "Sprint 3 #3: route heavy work (dream cycle, skill"
            " mutation eval) to the SleepWorker so it fires on idle"
            " thresholds instead of fixed cron intervals."
        ),
    ),
    FeatureFlag(
        name="cognition.reflection.materialize_to_disk",
        default=True,
        description=(
            "ReflectionMaterializer writes inner_monologue +"
            " metacog_proposal events into AGENTS.md / MEMORY.md."
            " Off = reflections live only on the bus."
        ),
    ),

    # ── Evolution ────────────────────────────────────────────────
    FeatureFlag(
        name="evolution.reflective_mutator",
        default=False,
        description=(
            "Sprint 3 GEPA-style reflective mutator. Off-by-default"
            " because weak-tier models produce noise. Flip on for"
            " strong-model installs that want skill mutation"
            " suggestions."
        ),
    ),
    FeatureFlag(
        name="evolution.reasoning_bank",
        default=False,
        description=(
            "Strategy distiller pulls bank-retrieved strategies into"
            " the user message. Off by default; LLM call is slow."
        ),
    ),
    # Wave-32+ (2026-05-18): auto-approve evolution proposals whose
    # confidence clears a threshold. Without this every proposal
    # piled up in the operator UI waiting for a click — even high-
    # confidence ones (conf 0.8+) the user invariably approved
    # anyway. Default ON @ 0.8 so the UI surface stays clean for
    # the genuinely-uncertain ones (conf < 0.8 still need review).
    FeatureFlag(
        name="evolution.auto_approve.enabled",
        default=True,
        description=(
            "Master switch. When true, proposals whose confidence"
            " is >= evolution.auto_approve.threshold are marked"
            " approved at trigger time instead of waiting for a"
            " human click. Lower-confidence proposals still pile"
            " up as pending."
        ),
    ),
    FeatureFlag(
        name="evolution.auto_approve.threshold",
        default=0.8,
        description=(
            "Confidence cutoff for auto-approval (0.0-1.0). At 0.8"
            " (default) this maps to the same cutoff the UI uses"
            " to render the success-tone badge. Lower the threshold"
            " for a daemon that should self-evolve aggressively;"
            " raise it for a conservative one."
        ),
    ),

    # ── Tool guard ───────────────────────────────────────────────
    FeatureFlag(
        name="tool_guard.persona_redirect",
        default=True,
        description=(
            "Wave-31 redirect: when file_read/file_write/apply_patch"
            " targets a persona-named file outside the persona dir,"
            " return a structured error pointing at update_persona."
            " Disable to restore plain not-found behaviour."
        ),
    ),
]


__all__ = ["BUILTIN_FLAGS"]
