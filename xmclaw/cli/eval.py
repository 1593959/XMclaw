"""``xmclaw eval ...`` CLI surface.

Three subcommands:

  * ``xmclaw eval list`` — list registered benchmark suites.
  * ``xmclaw eval run <suite_id> [--limit N] [--profile-id ID] [--out PATH]``
    — run a suite end-to-end and emit a JSON ``SuiteResult``.
  * ``xmclaw eval ab <baseline> <treatment> --suite <id> [--limit N]`` —
    run the same suite against two LLM profile ids and report the delta.
    This is the "A/B" payoff Sprint 4 promises.

The CLI builds an agent via ``xmclaw.daemon.factory.build_agent_from_config``
once per command invocation. Per-task isolation inside the run is the
Runner's job (see ``xmclaw.eval.harness.Runner._run_one``).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import typer

from xmclaw.eval import SUITE_REGISTRY, Runner, SuiteResult
from xmclaw.eval.harness import BenchmarkSuite

eval_app = typer.Typer(
    help="Run benchmark suites against the configured agent (Sprint 4 A/B).",
)


def _resolve_suite(suite_id: str) -> BenchmarkSuite:
    """Look up the suite by id, or exit with a friendly error."""
    cls = SUITE_REGISTRY.get(suite_id)
    if cls is None:
        ids = ", ".join(sorted(SUITE_REGISTRY.keys())) or "(none)"
        typer.echo(
            f"  [x]  unknown suite {suite_id!r}; registered: {ids}",
            err=True,
        )
        raise typer.Exit(code=2)
    return cls()


def _load_config_for_eval(config_path: str) -> dict[str, Any] | None:
    """Read the daemon config, or return None if absent. Same lookup
    spirit as ``xmclaw serve`` but simpler: we don't need the full search
    list because eval is meant to be run from the project root or with
    an explicit ``--config`` path."""
    p = Path(config_path)
    if not p.exists():
        typer.echo(
            f"  [!]   no config at {p} — run with a stub agent (text-only). "
            "Pass --config PATH for a real LLM run.",
            err=True,
        )
        return None
    try:
        from xmclaw.daemon.factory import load_config
        return load_config(p)
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"  [x]  failed to load config {p}: {exc}", err=True)
        raise typer.Exit(code=2) from exc


def _build_agent_factory(
    cfg: dict[str, Any] | None,
    profile_id: str | None,
) -> Any:
    """Return a callable ``() -> agent_like`` for the Runner.

    With no config, falls back to a deterministic stub agent (returns
    the empty string) so ``xmclaw eval run`` is at least exercisable
    end-to-end on a fresh checkout. Real benchmark runs need a config.
    """
    if cfg is None:
        def _stub_factory() -> Any:
            async def _arun(prompt: str) -> str:
                return ""
            return type("StubAgent", (), {"arun": staticmethod(_arun)})()
        return _stub_factory

    from xmclaw.core.bus import InProcessEventBus
    from xmclaw.daemon.factory import build_agent_from_config

    # CRITICAL isolation: point the workspace root at a throwaway dir for
    # the eval process. Benchmark prompts (e.g. LongMemEval's "User: I have
    # a golden retriever and I work as a dentist.") otherwise flow through
    # the agent's normal memory pipeline (journal / fact extraction / graph)
    # and get written into the USER's real ~/.xmclaw — then surface in real
    # chat as fabricated user facts. ``XMC_DATA_DIR`` redirects every store
    # (memory.db / graph.db / facts / journal / events) in one lever. Only
    # set it when the user hasn't already pinned one, and only for real
    # (config-backed) runs.
    import os
    import tempfile
    if not os.environ.get("XMC_DATA_DIR"):
        _iso_home = tempfile.mkdtemp(prefix="xmc_eval_home_")
        os.environ["XMC_DATA_DIR"] = _iso_home
        typer.echo(
            f"  [eval] isolated memory home → {_iso_home} "
            "(benchmark data will NOT touch ~/.xmclaw)"
        )

    def _real_factory() -> Any:
        bus = InProcessEventBus()
        agent = build_agent_from_config(cfg, bus)
        if agent is None:
            raise RuntimeError(
                "config has no LLM api_key — cannot run a real benchmark. "
                "Either set llm.<provider>.api_key in the config or run "
                "with no --config to use the stub agent."
            )
        # Profile pinning: when the caller wants to A/B, we wrap the
        # agent so every run_turn forces ``llm_profile_id`` = the chosen
        # profile. This lets the same registered suite drive both arms
        # of an A/B with no per-task plumbing.
        if profile_id is not None:
            original = agent.run_turn

            async def _run_turn_pinned(
                session_id: str, user_message: str, **kw: Any,
            ) -> Any:
                kw["llm_profile_id"] = profile_id
                return await original(session_id, user_message, **kw)

            agent.run_turn = _run_turn_pinned  # type: ignore[method-assign]
        return agent

    return _real_factory


def _write_output(result: SuiteResult, out: str | None) -> None:
    payload = json.dumps(result.to_dict(), ensure_ascii=False, indent=2)
    if out is None or out == "-":
        typer.echo(payload)
        return
    p = Path(out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(payload + "\n", encoding="utf-8")
    typer.echo(f"  [ok]  wrote {p} ({result.n_passed}/{result.n_tasks} passed)")


@eval_app.command("list")
def list_suites() -> None:
    """List the benchmark suites the harness knows about."""
    if not SUITE_REGISTRY:
        typer.echo("  (no suites registered)")
        return
    typer.echo(f"  [ok]  {len(SUITE_REGISTRY)} suite(s) registered:")
    for sid, cls in sorted(SUITE_REGISTRY.items()):
        doc = (cls.__doc__ or "").strip().splitlines()[0] if cls.__doc__ else ""
        typer.echo(f"    - {sid}: {doc}")


@eval_app.command("run")
def run_suite(
    suite_id: str = typer.Argument(..., help="Suite id (see `xmclaw eval list`)."),
    limit: int = typer.Option(0, help="Cap tasks to N (0 = whole suite)."),
    profile_id: str = typer.Option(
        "", "--profile-id",
        help="Pin the agent to this LLM profile id (per-suite A/B arm).",
    ),
    out: str = typer.Option(
        "", "--out",
        help="Where to write the SuiteResult JSON. Empty or '-' = stdout.",
    ),
    config: str = typer.Option(
        "daemon/config.json", "--config",
        help="Daemon config path. Missing → stub agent (zero-output).",
    ),
) -> None:
    """Run a single benchmark suite and emit a SuiteResult JSON."""
    suite = _resolve_suite(suite_id)
    cfg = _load_config_for_eval(config)
    factory = _build_agent_factory(cfg, profile_id or None)
    runner = Runner(factory, suite)
    result = asyncio.run(runner.run(limit=limit if limit > 0 else None))
    _write_output(result, out or None)


@eval_app.command("ab")
def ab_compare(
    baseline_profile: str = typer.Argument(..., help="Baseline LLM profile id."),
    treatment_profile: str = typer.Argument(..., help="Treatment LLM profile id."),
    suite: str = typer.Option(..., "--suite", help="Suite id."),
    limit: int = typer.Option(0, help="Cap tasks to N (0 = whole suite)."),
    out: str = typer.Option(
        "", "--out",
        help="Write the A/B JSON ({baseline, treatment, delta}) here. Empty = stdout.",
    ),
    config: str = typer.Option("daemon/config.json", "--config"),
) -> None:
    """Run the same suite against two profiles and report the delta."""
    suite_obj = _resolve_suite(suite)
    cfg = _load_config_for_eval(config)
    if cfg is None:
        typer.echo(
            "  [x]  ab requires a config — A/B-ing the stub agent against "
            "itself yields no signal.",
            err=True,
        )
        raise typer.Exit(code=2)
    n_limit = limit if limit > 0 else None

    async def _run_arm(profile: str) -> SuiteResult:
        factory = _build_agent_factory(cfg, profile)
        return await Runner(factory, suite_obj).run(limit=n_limit)

    async def _run_both() -> tuple[SuiteResult, SuiteResult]:
        # Sequential by design — running both arms in parallel would
        # contend on the same LLM rate-limit, distorting latency.
        baseline = await _run_arm(baseline_profile)
        treatment = await _run_arm(treatment_profile)
        return baseline, treatment

    baseline, treatment = asyncio.run(_run_both())
    delta = {
        "pass_rate": treatment.pass_rate - baseline.pass_rate,
        "mean_score": treatment.mean_score - baseline.mean_score,
        "total_cost_usd": treatment.total_cost_usd - baseline.total_cost_usd,
        "total_latency_s": treatment.total_latency_s - baseline.total_latency_s,
    }
    payload = {
        "suite_id": suite_obj.suite_id,
        "baseline_profile": baseline_profile,
        "treatment_profile": treatment_profile,
        "baseline": baseline.to_dict(),
        "treatment": treatment.to_dict(),
        "delta": delta,
    }
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if out and out != "-":
        p = Path(out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(rendered + "\n", encoding="utf-8")
        typer.echo(
            f"  [ok]  wrote {p}; "
            f"Δpass_rate = {delta['pass_rate']:+.3f}, "
            f"Δmean_score = {delta['mean_score']:+.3f}"
        )
    else:
        typer.echo(rendered)


__all__ = ["eval_app"]
