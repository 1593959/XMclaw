"""Plugin registry for ``xmclaw doctor`` (Epic #10).

The doctor is a sequence of discrete *checks*. Originally each check
was a pure function in :mod:`xmclaw.cli.doctor`; this registry layer
makes the set extensible so third-party packages can ship their own
checks via the ``xmclaw.doctor`` entry-point group.

Design notes:

* One :class:`DoctorCheck` = one inspection (no suite-of-checks class).
  Checks are cheap, explicit, and produced top-down by the registry's
  iteration order; bundling them hides dependencies.

* Checks may share expensive work through the mutable
  :class:`DoctorContext`: the first check to load the config caches it
  on ``ctx.cfg`` so subsequent checks can read it without re-parsing.

* Fixes are OPT-IN per check via :meth:`DoctorCheck.fix`. The default
  is no-op / returns False. A fix runner lives in a follow-up phase
  of this Epic; this module only exposes the API.

* Discovery uses :func:`importlib.metadata.entry_points` under the
  group ``xmclaw.doctor``. Each entry-point must resolve to a
  :class:`DoctorCheck` subclass (or factory returning one). Failures
  to load are caught and surfaced as a synthetic failing check — a
  broken plugin must not take down the whole diagnosis.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, ClassVar


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One check's outcome.

    ``ok=True`` => green; ``ok=False`` => red. A non-None ``advisory``
    prints under the main line and is also how a check signals that
    :meth:`DoctorCheck.fix` may have something useful to do.
    """

    name: str
    ok: bool
    detail: str
    advisory: str | None = None
    fix_available: bool = False

    def render(self) -> str:
        # ASCII icons only — the unicode check/cross crash GBK consoles.
        icon = "[ok]" if self.ok else ("[!]" if self.advisory else "[x]")
        line = f"  {icon} {self.name}: {self.detail}"
        if self.advisory:
            line += f"\n    -> {self.advisory}"
        return line

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "advisory": self.advisory,
            "fix_available": self.fix_available,
        }


@dataclass
class DoctorContext:
    """Inputs the checks need + a scratchpad for shared state."""

    config_path: Path
    host: str = "127.0.0.1"
    port: int = 8765
    probe_daemon: bool = True
    cfg: dict[str, Any] | None = None          # filled by ConfigCheck
    token_path: Path | None = None              # override, else default
    extras: dict[str, Any] = field(default_factory=dict)


class DoctorCheck(ABC):
    """Base class for a doctor check.

    Subclasses declare :pyattr:`id` and :pyattr:`name` and implement
    :meth:`run`. :meth:`fix` defaults to a no-op so simple checks
    without a remediation step don't have to override it.
    """

    #: Machine identifier (stable, unique). Used in ``--json`` output
    #: and when another check wants to refer to this one.
    id: ClassVar[str] = ""

    #: Human-readable name shown in the terminal.
    name: ClassVar[str] = ""

    @abstractmethod
    def run(self, ctx: DoctorContext) -> CheckResult:
        """Inspect ``ctx`` and return a verdict. Must not raise."""

    def fix(self, ctx: DoctorContext) -> bool:  # noqa: ARG002
        """Attempt to remediate a failed check. Return True on success.

        Default: no-op. Only override for checks that can repair
        themselves (e.g. ``mkdir`` a missing workspace).
        """
        return False


class DoctorRegistry:
    """Ordered set of checks. Built-in first, plugins appended.

    The order matters: config-parsing checks must run before any check
    that reads ``ctx.cfg``. The registry preserves insertion order so
    callers can rely on it.
    """

    ENTRY_POINT_GROUP = "xmclaw.doctor"

    def __init__(self) -> None:
        self._checks: list[DoctorCheck] = []

    def register(self, check: DoctorCheck) -> None:
        self._checks.append(check)

    def register_factory(self, factory: Callable[[], DoctorCheck]) -> None:
        self._checks.append(factory())

    def checks(self) -> list[DoctorCheck]:
        return list(self._checks)

    def run_all(self, ctx: DoctorContext) -> list[CheckResult]:
        results: list[CheckResult] = []
        for check in self._checks:
            results.append(self._run_one(check, ctx))
        return results

    def _run_one(self, check: DoctorCheck, ctx: DoctorContext) -> CheckResult:
        try:
            return check.run(ctx)
        except Exception as exc:  # noqa: BLE001
            return CheckResult(
                name=check.name or check.id or type(check).__name__,
                ok=False,
                detail=f"check raised {type(exc).__name__}: {exc}",
                advisory="this is a bug in the check itself",
            )

    @dataclass(frozen=True, slots=True)
    class FixAttempt:
        """Record of one fix attempt. ``before`` is the failing result that
        triggered the fix, ``after`` is the re-run result (same check). A
        successful fix is one where ``after.ok`` is True."""

        check_id: str
        before: "CheckResult"
        after: "CheckResult"
        fix_raised: str | None = None   # exception message if fix() threw

    def run_fixes(
        self, ctx: DoctorContext, results: list[CheckResult],
    ) -> list["DoctorRegistry.FixAttempt"]:
        """For every failing result whose check advertises ``fix_available``,
        call :meth:`DoctorCheck.fix` and re-run the check. Returns the list
        of attempts (one per fixable failing check) so callers can show a
        summary and pick the new overall verdict.

        Matches results back to checks by ``check.id`` — checks without an
        ``id`` (unusual) are skipped to keep the mapping unambiguous.
        """
        attempts: list[DoctorRegistry.FixAttempt] = []
        by_id = {c.id: c for c in self._checks if c.id}
        name_to_id = {c.name: c.id for c in self._checks if c.id and c.name}
        for before in results:
            if before.ok or not before.fix_available:
                continue
            # The CLI re-exports ``CheckResult`` from ``cli.doctor`` so the
            # ``name`` field is the only reliable handle back to the check.
            cid = name_to_id.get(before.name)
            if cid is None:
                continue
            check = by_id.get(cid)
            if check is None:
                continue
            fix_raised: str | None = None
            try:
                check.fix(ctx)
            except Exception as exc:  # noqa: BLE001
                fix_raised = f"{type(exc).__name__}: {exc}"
            after = self._run_one(check, ctx)
            attempts.append(DoctorRegistry.FixAttempt(
                check_id=cid,
                before=before,
                after=after,
                fix_raised=fix_raised,
            ))
        return attempts

    def discover_plugins(self) -> list[CheckResult]:
        """Load third-party checks via the ``xmclaw.doctor`` entry-point
        group. Returns synthetic failure :class:`CheckResult` s for any
        plugin that couldn't be imported — a broken plugin must not
        kill the whole doctor pass.
        """
        errors: list[CheckResult] = []
        from importlib.metadata import entry_points

        try:
            eps = entry_points(group=self.ENTRY_POINT_GROUP)
        except TypeError:
            # Python 3.9 fallback (we target 3.10+ but be defensive).
            eps = entry_points().get(self.ENTRY_POINT_GROUP, [])  # type: ignore[assignment]

        for ep in eps:
            try:
                obj = ep.load()
            except Exception as exc:  # noqa: BLE001
                errors.append(CheckResult(
                    name=f"plugin:{ep.name}",
                    ok=False,
                    detail=f"failed to import: {type(exc).__name__}: {exc}",
                    advisory=f"uninstall the broken package or fix entry point {ep.value}",
                ))
                continue
            instance: DoctorCheck | None = None
            if isinstance(obj, type) and issubclass(obj, DoctorCheck):
                try:
                    instance = obj()
                except Exception as exc:  # noqa: BLE001
                    errors.append(CheckResult(
                        name=f"plugin:{ep.name}",
                        ok=False,
                        detail=f"constructor raised: {exc}",
                    ))
                    continue
            elif callable(obj):
                try:
                    candidate = obj()
                except Exception as exc:  # noqa: BLE001
                    errors.append(CheckResult(
                        name=f"plugin:{ep.name}",
                        ok=False,
                        detail=f"factory raised: {exc}",
                    ))
                    continue
                if isinstance(candidate, DoctorCheck):
                    instance = candidate
            if instance is None:
                errors.append(CheckResult(
                    name=f"plugin:{ep.name}",
                    ok=False,
                    detail="entry point did not resolve to a DoctorCheck",
                    advisory=f"got {type(obj).__name__} from {ep.value}",
                ))
                continue
            self._checks.append(instance)
        return errors


# ── built-in checks (thin wrappers over the pure functions in cli.doctor) ──


def _load_cfg_on_ctx(ctx: DoctorContext) -> CheckResult:
    """Shared helper for :class:`ConfigCheck` — caches the parsed dict
    on ``ctx.cfg`` for downstream checks."""
    from xmclaw.cli.doctor import check_config_file

    result_dataclass, cfg = check_config_file(ctx.config_path)
    ctx.cfg = cfg
    return CheckResult(
        name=result_dataclass.name,
        ok=result_dataclass.ok,
        detail=result_dataclass.detail,
        advisory=result_dataclass.advisory,
    )


class ConfigCheck(DoctorCheck):
    id = "config"
    name = "config"

    def run(self, ctx: DoctorContext) -> CheckResult:
        return _load_cfg_on_ctx(ctx)


class LLMCheck(DoctorCheck):
    id = "llm"
    name = "llm"

    def run(self, ctx: DoctorContext) -> CheckResult:
        if ctx.cfg is None:
            return CheckResult(
                name=self.name, ok=False,
                detail="skipped: config not parsed",
            )
        from xmclaw.cli.doctor import check_llm_configured

        r = check_llm_configured(ctx.cfg)
        return CheckResult(
            name=r.name, ok=r.ok, detail=r.detail, advisory=r.advisory,
        )


class ToolsCheck(DoctorCheck):
    id = "tools"
    name = "tools"

    def run(self, ctx: DoctorContext) -> CheckResult:
        if ctx.cfg is None:
            return CheckResult(
                name=self.name, ok=False,
                detail="skipped: config not parsed",
            )
        from xmclaw.cli.doctor import check_tools_configured

        r = check_tools_configured(ctx.cfg)
        return CheckResult(
            name=r.name, ok=r.ok, detail=r.detail, advisory=r.advisory,
        )


class PairingCheck(DoctorCheck):
    id = "pairing"
    name = "pairing"

    def run(self, ctx: DoctorContext) -> CheckResult:
        from xmclaw.cli.doctor import check_pairing_token
        from xmclaw.daemon.pairing import default_token_path

        r = check_pairing_token(ctx.token_path or default_token_path())
        return CheckResult(
            name=r.name, ok=r.ok, detail=r.detail, advisory=r.advisory,
        )


class PortCheck(DoctorCheck):
    id = "port"
    name = "port"

    def run(self, ctx: DoctorContext) -> CheckResult:
        from xmclaw.cli.doctor import check_port_available

        r = check_port_available(ctx.host, ctx.port)
        return CheckResult(
            name=r.name, ok=r.ok, detail=r.detail, advisory=r.advisory,
        )


class WorkspaceCheck(DoctorCheck):
    """Verify ``~/.xmclaw/v2/`` exists and is writable.

    Most other components (pairing token file, events DB, daemon PID file)
    live under this directory. A missing or read-only workspace is the root
    cause behind several noisier failures, so we flag it first and offer a
    one-shot ``fix()`` that creates it.
    """

    id = "workspace"
    name = "workspace"

    #: Honors the same env var as :func:`xmclaw.daemon.pairing.default_token_path`
    #: when set — test harnesses set it to an isolated tmp dir.
    def _target(self, ctx: DoctorContext) -> Path:
        override = ctx.extras.get("workspace_dir")
        if isinstance(override, (str, Path)):
            return Path(override)
        return Path.home() / ".xmclaw" / "v2"

    def run(self, ctx: DoctorContext) -> CheckResult:
        target = self._target(ctx)
        if not target.exists():
            return CheckResult(
                name=self.name, ok=False,
                detail=f"workspace not found: {target}",
                advisory=f"run 'xmclaw doctor --fix' to create {target}",
                fix_available=True,
            )
        if not target.is_dir():
            return CheckResult(
                name=self.name, ok=False,
                detail=f"workspace path exists but is not a directory: {target}",
                advisory="remove or rename the conflicting file",
                # Not auto-fixable — removing a file the user created is risky.
                fix_available=False,
            )
        if not os.access(target, os.W_OK):
            return CheckResult(
                name=self.name, ok=False,
                detail=f"workspace not writable: {target}",
                advisory="check directory permissions (chmod u+w)",
                fix_available=False,
            )
        return CheckResult(
            name=self.name, ok=True,
            detail=f"workspace ready: {target}",
        )

    def fix(self, ctx: DoctorContext) -> bool:
        target = self._target(ctx)
        if target.exists():
            return target.is_dir() and os.access(target, os.W_OK)
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError:
            return False
        return True


class DaemonHealthCheck(DoctorCheck):
    id = "daemon"
    name = "daemon"

    def run(self, ctx: DoctorContext) -> CheckResult:
        if not ctx.probe_daemon:
            return CheckResult(
                name=self.name, ok=True,
                detail="skipped (--no-daemon-probe)",
            )
        from xmclaw.cli.doctor import check_daemon_health

        r = check_daemon_health(ctx.host, ctx.port)
        return CheckResult(
            name=r.name, ok=r.ok, detail=r.detail, advisory=r.advisory,
        )


def build_default_registry() -> DoctorRegistry:
    """Return a registry populated with the built-in checks.

    Order matters: ConfigCheck must run first so subsequent checks
    can read ``ctx.cfg``.
    """
    reg = DoctorRegistry()
    reg.register(ConfigCheck())
    reg.register(LLMCheck())
    reg.register(ToolsCheck())
    reg.register(WorkspaceCheck())
    reg.register(PairingCheck())
    reg.register(PortCheck())
    reg.register(DaemonHealthCheck())
    return reg
