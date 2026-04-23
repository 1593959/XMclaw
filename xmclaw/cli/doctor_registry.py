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
    # Off by default — the doctor otherwise does no outbound HTTP, so
    # it stays runnable on air-gapped machines and in CI without a
    # surprise network dependency. The CLI opts in with ``--network``.
    probe_network: bool = False
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

    #: Delegates to :func:`xmclaw.utils.paths.v2_workspace_dir` so
    #: ``XMC_DATA_DIR`` (the §3.1 workspace-root lever) reroutes this
    #: check alongside everything else. Test harnesses can still pin a
    #: specific path via ``ctx.extras["workspace_dir"]``.
    def _target(self, ctx: DoctorContext) -> Path:
        override = ctx.extras.get("workspace_dir")
        if isinstance(override, (str, Path)):
            return Path(override)
        from xmclaw.utils.paths import v2_workspace_dir
        return v2_workspace_dir()

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


class ConnectivityCheck(DoctorCheck):
    """Probe reachability of configured LLM endpoints.

    Skipped unless :pyattr:`DoctorContext.probe_network` is True (CLI
    ``--network`` flag) — making HTTP calls from the doctor would
    otherwise break CI and offline-dev setups by surprise.

    Contract details worth pinning:

    * **No credentials leave the box.** We issue an unauthenticated
      ``HEAD`` request to the provider's base URL. A 2xx/3xx/4xx
      response means "DNS + TCP + TLS all worked" — the API key is
      still LLMCheck's problem, not ours. A 5xx or network error is
      the actual failure signal.
    * **Short timeout.** 5 s. A hung probe wastes more wall-time
      than it's worth.
    * **Uses stdlib ``urllib``**. Keeping the doctor free of an
      ``httpx`` dependency means the check can run before the
      installed extras are confirmed.
    * **Honors ``base_url`` overrides.** A user pointing at a proxy or
      self-hosted compatible endpoint should probe *that*, not the
      upstream. Same resolution order as the provider classes.

    Endpoints (fallbacks if ``base_url`` not set):
    - ``anthropic``: ``https://api.anthropic.com``
    - ``openai``: ``https://api.openai.com``
    """

    id = "connectivity"
    name = "connectivity"

    _DEFAULT_ENDPOINTS: ClassVar[dict[str, str]] = {
        "anthropic": "https://api.anthropic.com",
        "openai": "https://api.openai.com",
    }

    def _probe(self, url: str, timeout: float = 5.0) -> tuple[bool, str]:
        """Return ``(reachable, detail)``.

        Treats any HTTP status code as "reachable" — a 401/403 means
        the TLS handshake + auth challenge both worked, which is
        exactly what we're trying to verify. Only URLError (DNS /
        connect / TLS failure) and socket timeout count as unreachable.
        """
        import socket
        import urllib.request
        import urllib.error

        req = urllib.request.Request(url, method="HEAD")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return True, f"HTTP {resp.status}"
        except urllib.error.HTTPError as e:
            # Server spoke HTTP — that's reachable for our purposes.
            return True, f"HTTP {e.code}"
        except urllib.error.URLError as e:
            return False, f"URLError: {e.reason}"
        except socket.timeout:
            return False, f"timeout after {timeout:.0f}s"
        except OSError as e:
            return False, f"OSError: {e}"

    def run(self, ctx: DoctorContext) -> CheckResult:
        if not ctx.probe_network:
            return CheckResult(
                name=self.name, ok=True,
                detail="skipped (pass --network to probe LLM endpoints)",
            )
        if ctx.cfg is None:
            return CheckResult(
                name=self.name, ok=False,
                detail="skipped: config not parsed",
            )
        llm = ctx.cfg.get("llm")
        if not isinstance(llm, dict):
            return CheckResult(
                name=self.name, ok=True,
                detail="no 'llm' section — nothing to probe",
            )

        targets: list[tuple[str, str]] = []
        for provider, default_url in self._DEFAULT_ENDPOINTS.items():
            p = llm.get(provider)
            if not isinstance(p, dict) or not p.get("api_key"):
                continue
            url = p.get("base_url") or default_url
            targets.append((provider, url))

        if not targets:
            return CheckResult(
                name=self.name, ok=True,
                detail="no configured providers with api_key — nothing to probe",
            )

        results: list[tuple[str, bool, str]] = []
        for provider, url in targets:
            reachable, detail = self._probe(url)
            results.append((provider, reachable, f"{url} -> {detail}"))

        failures = [r for r in results if not r[1]]
        summary = "; ".join(f"{p}: {d}" for p, _, d in results)
        if failures:
            return CheckResult(
                name=self.name, ok=False,
                detail=f"{len(failures)}/{len(results)} provider(s) unreachable",
                advisory=f"check network + proxy settings. details: {summary}",
            )
        return CheckResult(
            name=self.name, ok=True,
            detail=f"{len(results)} provider(s) reachable ({summary})",
        )


class EventsDbCheck(DoctorCheck):
    """Probe ``~/.xmclaw/v2/events.db`` health.

    Covers the three practical failure modes:

    1. File absent — OK, the daemon creates it on first start. We report
       "not yet created" so the user understands there's nothing wrong.
    2. File present but the SQLite header is garbage or the file is
       locked — fail with the library error verbatim so the user can
       stop the process holding it / restore from backup.
    3. File present, opens, but ``PRAGMA user_version`` is newer than
       the code we're running — flag as an advisory (downgrade path
       isn't supported) rather than an outright fail, so a user who
       pinned an older wheel after touching main gets a clear message.

    Uses ``XMC_V2_EVENTS_DB_PATH`` when set (matches
    :func:`xmclaw.core.bus.default_events_db_path`), so the check is
    testable against a tmp file.
    """

    id = "events_db"
    name = "events_db"

    def _target(self, ctx: DoctorContext) -> Path:
        override = ctx.extras.get("events_db_path")
        if isinstance(override, (str, Path)):
            return Path(override)
        from xmclaw.core.bus import default_events_db_path

        return default_events_db_path()

    def run(self, ctx: DoctorContext) -> CheckResult:
        path = self._target(ctx)
        if not path.exists():
            return CheckResult(
                name=self.name, ok=True,
                detail=f"not yet created (will be created on `xmclaw start`): {path}",
            )
        if not path.is_file():
            return CheckResult(
                name=self.name, ok=False,
                detail=f"path exists but is not a file: {path}",
                advisory="remove or rename the conflicting entry",
            )
        import sqlite3

        from xmclaw.core.bus.sqlite import SCHEMA_VERSION

        try:
            # read-only mode — we never want the doctor to migrate or lock.
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            return CheckResult(
                name=self.name, ok=False,
                detail=f"cannot open {path.name}: {exc}",
                advisory="check that no other process has the DB locked; "
                         "if the file is corrupt, back it up and let the "
                         "daemon recreate it on next start",
            )
        try:
            row = conn.execute("PRAGMA user_version").fetchone()
            user_version = int(row[0]) if row else 0
        except sqlite3.Error as exc:
            conn.close()
            return CheckResult(
                name=self.name, ok=False,
                detail=f"events.db looks malformed: {exc}",
                advisory="back up and remove the file; the daemon "
                         "will recreate it on next start",
            )
        conn.close()
        if user_version > SCHEMA_VERSION:
            return CheckResult(
                name=self.name, ok=False,
                detail=f"events.db schema v{user_version} is newer than "
                       f"code v{SCHEMA_VERSION}",
                advisory="a newer xmclaw wrote this DB; upgrade the "
                         "installed package or point XMC_V2_EVENTS_DB_PATH "
                         "at a fresh file",
            )
        return CheckResult(
            name=self.name, ok=True,
            detail=f"events.db v{user_version} at {path}",
        )


class RoadmapLintCheck(DoctorCheck):
    """Run ``scripts/lint_roadmap.py`` against ``docs/DEV_ROADMAP.md``.

    Cheap drift-detector (§3.6.5): if an Epic is marked done but its
    end-date is blank, or a Milestone's exit criterion is unchecked
    despite its Epic being done, the linter returns a non-empty
    violation list. Surfacing this as a doctor check means anyone
    running ``xmclaw doctor`` before committing gets the same signal
    CI would produce later.

    Only runs when both the script and the roadmap exist in the
    current checkout — a released wheel won't ship the script, so
    the check quietly passes there.
    """

    id = "roadmap_lint"
    name = "roadmap_lint"

    def _paths(self) -> tuple[Path, Path] | None:
        # Walk up from this file until we find either DEV_ROADMAP.md or
        # run out of parents. Works from source checkout and from the
        # worktree arrangement without hardcoding either layout.
        here = Path(__file__).resolve()
        for parent in [here, *here.parents]:
            script = parent / "scripts" / "lint_roadmap.py"
            roadmap = parent / "docs" / "DEV_ROADMAP.md"
            if script.exists() and roadmap.exists():
                return script, roadmap
        return None

    def run(self, ctx: DoctorContext) -> CheckResult:
        paths = self._paths()
        if paths is None:
            return CheckResult(
                name=self.name, ok=True,
                detail="skipped (script/roadmap not present — released wheel)",
            )
        script, roadmap = paths
        import importlib.util
        import sys as _sys

        spec = importlib.util.spec_from_file_location("lint_roadmap", script)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        # Register before exec so ``@dataclass`` annotations can resolve
        # the owning module (Python 3.10 looks up module globals via
        # ``sys.modules`` during class body execution).
        _sys.modules["lint_roadmap"] = module
        spec.loader.exec_module(module)
        violations = module.lint(roadmap)
        if not violations:
            return CheckResult(
                name=self.name, ok=True,
                detail=f"{roadmap.name} clean",
            )
        # Surface up to the first 3 violations in the advisory so the user
        # can act without spelunking; the full list is available via
        # ``python scripts/lint_roadmap.py``.
        preview = "; ".join(violations[:3])
        if len(violations) > 3:
            preview += f" (+{len(violations) - 3} more)"
        return CheckResult(
            name=self.name, ok=False,
            detail=f"{len(violations)} roadmap violation(s)",
            advisory=f"run 'python scripts/lint_roadmap.py' — {preview}",
        )


class StalePidCheck(DoctorCheck):
    """Detect and clean up an orphaned ``daemon.pid`` file.

    If a previous daemon crashed without cleanup, ``~/.xmclaw/v2/daemon.pid``
    points at a process that no longer exists. ``xmclaw start`` refuses
    to spawn when that file is present, so users hit a confusing "daemon
    already running" error. This check catches that state up front and
    offers a one-shot ``fix()`` that deletes the stale ``daemon.pid`` +
    ``daemon.meta`` pair.

    States:
      * No PID file       -> OK "no daemon tracked"
      * PID file + alive  -> OK "daemon running (pid=N)"
      * PID file + dead   -> FAIL + fixable "stale pid file"

    Honors the ``XMC_V2_PID_PATH`` env var (same rule
    :func:`xmclaw.daemon.lifecycle.default_pid_path` uses) and the
    ``ctx.extras["pid_path"]`` override so tests can point it at a
    tmp file.
    """

    id = "pid_lock"
    name = "pid_lock"

    def _target(self, ctx: DoctorContext) -> Path:
        override = ctx.extras.get("pid_path")
        if isinstance(override, (str, Path)):
            return Path(override)
        from xmclaw.daemon.lifecycle import default_pid_path

        return default_pid_path()

    def run(self, ctx: DoctorContext) -> CheckResult:
        pid_path = self._target(ctx)
        if not pid_path.exists():
            return CheckResult(
                name=self.name, ok=True,
                detail="no daemon tracked",
            )
        try:
            pid = int(pid_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError) as exc:
            # Malformed file counts as stale — remove it.
            return CheckResult(
                name=self.name, ok=False,
                detail=f"pid file malformed: {exc}",
                advisory=f"run 'xmclaw doctor --fix' to clear {pid_path}",
                fix_available=True,
            )
        from xmclaw.daemon.lifecycle import _process_alive

        if _process_alive(pid):
            return CheckResult(
                name=self.name, ok=True,
                detail=f"daemon running (pid={pid})",
            )
        return CheckResult(
            name=self.name, ok=False,
            detail=f"stale pid file — pid {pid} is not running",
            advisory=(
                f"run 'xmclaw doctor --fix' to clear {pid_path}, "
                "or 'xmclaw stop' then 'xmclaw start'"
            ),
            fix_available=True,
        )

    def fix(self, ctx: DoctorContext) -> bool:
        pid_path = self._target(ctx)
        meta_path = pid_path.with_name("daemon.meta")
        try:
            pid_path.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)
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
    reg.register(EventsDbCheck())
    reg.register(ConnectivityCheck())
    reg.register(RoadmapLintCheck())
    reg.register(StalePidCheck())
    reg.register(DaemonHealthCheck())
    return reg
