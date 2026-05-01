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
import time
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
    """Parse ``daemon/config.json``; cache parsed dict on ``ctx.cfg``.

    Only the "file doesn't exist" failure mode is auto-fixable. Invalid
    JSON or a non-object root require user-visible inspection -- silently
    overwriting a file the user just edited would destroy their work.
    When missing, ``fix()`` writes the same minimum-viable skeleton that
    ``xmclaw config init`` uses, so the two paths stay in lockstep.
    """

    id = "config"
    name = "config"

    def run(self, ctx: DoctorContext) -> CheckResult:
        result = _load_cfg_on_ctx(ctx)
        if result.ok:
            return result
        fixable = not ctx.config_path.exists()
        if not fixable:
            return result
        extra = f"or run 'xmclaw doctor --fix' to write a skeleton at {ctx.config_path}"
        advisory = f"{result.advisory}; {extra}" if result.advisory else extra
        return CheckResult(
            name=result.name, ok=False, detail=result.detail,
            advisory=advisory, fix_available=True,
        )

    def fix(self, ctx: DoctorContext) -> bool:
        if ctx.config_path.exists():
            # Never overwrite a user-created file from the doctor path.
            return False
        import json as _json
        from xmclaw.cli.config_template import default_config_template
        try:
            ctx.config_path.parent.mkdir(parents=True, exist_ok=True)
            ctx.config_path.write_text(
                _json.dumps(default_config_template(), indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            return False
        return True


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
    """Inspect ~/.xmclaw/v2/pairing_token.txt.

    Two failure modes are safely auto-fixable:

    * **Empty token file** -- unlink it so the next ``xmclaw serve`` can
      regenerate a fresh token. Any paired clients were already broken
      (an empty token matches nothing), so there's no regression risk.
    * **Loose POSIX perms** (any of group/other bits set) -- ``chmod 600``
      preserves the token itself while locking it down to the owning user.

    Everything else (unreadable, path missing entirely) is either not a
    failure or not safely remediable without user intent.
    """

    id = "pairing"
    name = "pairing"

    def _target(self, ctx: DoctorContext) -> Path:
        if ctx.token_path is not None:
            return ctx.token_path
        from xmclaw.daemon.pairing import default_token_path
        return default_token_path()

    def _fixable_state(self, path: Path) -> str | None:
        """Return ``"empty"``, ``"loose_perms"``, or ``None``.

        Kept in one place so :meth:`run` and :meth:`fix` agree on which
        failure modes are auto-remediable.
        """
        if not path.exists():
            return None
        try:
            content = path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if content == "":
            return "empty"
        import sys
        if sys.platform == "win32":
            return None
        try:
            mode = os.stat(path).st_mode & 0o777
        except OSError:
            return None
        if mode & 0o077:
            return "loose_perms"
        return None

    def run(self, ctx: DoctorContext) -> CheckResult:
        from xmclaw.cli.doctor import check_pairing_token

        path = self._target(ctx)
        r = check_pairing_token(path)
        if r.ok:
            return CheckResult(
                name=r.name, ok=True, detail=r.detail, advisory=r.advisory,
            )
        fixable = self._fixable_state(path) is not None
        advisory = r.advisory
        if fixable:
            extra = f"run 'xmclaw doctor --fix' to repair {path}"
            advisory = f"{advisory}; {extra}" if advisory else extra
        return CheckResult(
            name=r.name, ok=False, detail=r.detail,
            advisory=advisory, fix_available=fixable,
        )

    def fix(self, ctx: DoctorContext) -> bool:
        path = self._target(ctx)
        state = self._fixable_state(path)
        if state == "empty":
            try:
                path.unlink()
            except OSError:
                return False
            return True
        if state == "loose_perms":
            try:
                os.chmod(path, 0o600)
            except OSError:
                return False
            return True
        return False


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


class MemoryDbCheck(DoctorCheck):
    """Probe ``~/.xmclaw/v2/memory.db`` health (Epic #5 sibling of events.db).

    Mirrors :class:`EventsDbCheck`'s verdict shape but for the sqlite-vec
    memory store. Four practical states:

    1. Memory layer disabled in config (``memory.enabled: false``) — OK,
       nothing to probe.
    2. File absent — OK, ``SqliteVecMemory`` creates it lazily on first
       put. Report "not yet created" so the user isn't alarmed.
    3. File present but not a file / unopenable / corrupt SQLite — fail
       with the library error so the user knows what to clean up.
    4. File opens but does not contain a ``memory_items`` table — fail
       as "not a memory.db" so we don't mis-diagnose a foreign SQLite file.

    The override path (``ctx.extras["memory_db_path"]``) mirrors
    :class:`EventsDbCheck` so tests can redirect to a tmp file without
    touching the real workspace.
    """

    id = "memory_db"
    name = "memory_db"

    def _target(self, ctx: DoctorContext) -> Path | None:
        """Return the path to probe, or ``None`` if memory is disabled.

        Precedence (highest first):
          1. ``ctx.extras["memory_db_path"]`` — test override.
          2. ``cfg["memory"]["db_path"]`` — user-configured absolute path.
          3. :func:`xmclaw.utils.paths.default_memory_db_path`.
        """
        override = ctx.extras.get("memory_db_path")
        if isinstance(override, (str, Path)):
            return Path(override)
        cfg = ctx.cfg or {}
        mem_cfg = cfg.get("memory")
        if isinstance(mem_cfg, dict):
            if mem_cfg.get("enabled") is False:
                return None
            cfg_path = mem_cfg.get("db_path")
            if isinstance(cfg_path, str) and cfg_path and cfg_path != ":memory:":
                return Path(cfg_path)
        from xmclaw.utils.paths import default_memory_db_path

        return default_memory_db_path()

    def run(self, ctx: DoctorContext) -> CheckResult:
        path = self._target(ctx)
        if path is None:
            return CheckResult(
                name=self.name, ok=True,
                detail="memory disabled in config (memory.enabled: false)",
            )
        if not path.exists():
            return CheckResult(
                name=self.name, ok=True,
                detail=f"not yet created (will be created on first put): {path}",
            )
        if not path.is_file():
            return CheckResult(
                name=self.name, ok=False,
                detail=f"path exists but is not a file: {path}",
                advisory="remove or rename the conflicting entry",
            )
        import sqlite3

        try:
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
            row = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='memory_items'"
            ).fetchone()
        except sqlite3.Error as exc:
            conn.close()
            return CheckResult(
                name=self.name, ok=False,
                detail=f"memory.db looks malformed: {exc}",
                advisory="back up and remove the file; the daemon "
                         "will recreate it on next start",
            )
        if row is None:
            # Count items if table exists; otherwise signal wrong file.
            conn.close()
            return CheckResult(
                name=self.name, ok=False,
                detail=f"{path.name} exists but has no memory_items table",
                advisory="this file isn't an xmclaw memory.db; back it up "
                         "and remove it so the daemon can recreate it",
            )
        try:
            count_row = conn.execute(
                "SELECT COUNT(*) FROM memory_items"
            ).fetchone()
            count = int(count_row[0]) if count_row else 0
        except sqlite3.Error:
            count = -1

        # B-59: read the vec table's declared dim and compare against
        # configured embedding.dimensions. SqliteVecMemory raises
        # RuntimeError on mismatch at first put — better to surface
        # here so the user can fix config (or wipe the DB) before
        # the daemon ever tries to ingest.
        existing_dim: int | None = None
        try:
            vec_row = conn.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type='table' AND name='memory_vec'"
            ).fetchone()
            # connect() default row_factory returns a tuple — access
            # by index, not key.
            sql = vec_row[0] if vec_row else None
            if sql and "float[" in sql and "]" in sql.split("float[")[1]:
                existing_dim = int(sql.split("float[")[1].split("]")[0])
        except (sqlite3.Error, ValueError, IndexError, TypeError):
            existing_dim = None

        conn.close()

        cfg = ctx.cfg or {}
        emb_section = (((cfg.get("evolution") or {}).get("memory") or {})
                       .get("embedding") or {})
        configured_dim = emb_section.get("dimensions")
        if (
            existing_dim is not None
            and configured_dim is not None
            and int(configured_dim) != existing_dim
        ):
            return CheckResult(
                name=self.name, ok=False,
                detail=(
                    f"embedding dim mismatch: memory.db has "
                    f"{existing_dim}-D vectors but config says "
                    f"{int(configured_dim)}-D"
                ),
                advisory=(
                    f"either set evolution.memory.embedding.dimensions="
                    f"{existing_dim} OR delete {path.name} (and the "
                    f"daemon will rebuild it on next put). The current "
                    f"setup will crash on the first agent write."
                ),
            )

        if count < 0:
            return CheckResult(
                name=self.name, ok=True,
                detail=f"memory.db present at {path} (count unavailable)",
            )
        dim_label = f", {existing_dim}-D" if existing_dim else ""
        return CheckResult(
            name=self.name, ok=True,
            detail=f"memory.db healthy at {path} ({count} item(s){dim_label})",
        )


class MemoryProviderCheck(DoctorCheck):
    """Verify the live agent has a MemoryManager + at least the
    BuiltinFileMemoryProvider registered (B-30).

    Probes ``app.state.agent._memory_manager`` via a daemon HTTP call
    when the daemon's running; returns "skip" when the daemon is off.
    """

    id = "memory_providers"
    name = "memory_providers"

    def run(self, ctx: DoctorContext) -> CheckResult:
        if not ctx.probe_daemon:
            return CheckResult(
                name=self.name, ok=True,
                detail="skipped (probe_daemon=False)",
            )
        try:
            import json as _json
            import urllib.request as _ur
            import urllib.error as _ue
            url = f"http://{ctx.host}:{ctx.port}/api/v2/memory/providers"
            try:
                token = ctx.token_path.read_text(encoding="utf-8").strip() if ctx.token_path else ""
            except OSError:
                token = ""
            if token:
                url += f"?token={token}"
            with _ur.urlopen(url, timeout=3) as r:
                data = _json.loads(r.read().decode("utf-8", "replace"))
        except (_ue.URLError, OSError, _json.JSONDecodeError):
            return CheckResult(
                name=self.name, ok=True,
                detail="daemon unreachable (skipped)",
            )
        except Exception as exc:  # noqa: BLE001
            return CheckResult(
                name=self.name, ok=False,
                detail=f"endpoint failed: {exc}",
            )
        if not data.get("wired"):
            return CheckResult(
                name=self.name, ok=False,
                detail="MemoryManager not wired",
                advisory="agent boot may have failed; check daemon.log",
            )
        provs = data.get("providers", [])
        names = [p.get("name", "?") for p in provs]
        if "builtin" not in names:
            return CheckResult(
                name=self.name, ok=False,
                detail=f"missing 'builtin' provider; found: {names}",
                advisory="BuiltinFileMemoryProvider should always be registered",
            )
        external = [n for n in names if n != "builtin"]
        if len(external) > 1:
            return CheckResult(
                name=self.name, ok=False,
                detail=f"multiple external providers: {external}",
                advisory="manager should reject duplicates — investigate",
            )
        return CheckResult(
            name=self.name, ok=True,
            detail=f"providers wired: {names}",
        )


class MemoryProviderConfigCheck(DoctorCheck):
    """Verify the configured external memory provider is sane:
    ``evolution.memory.provider`` is one of the known names and any
    required credentials are present (B-30)."""

    id = "memory_provider_config"
    name = "memory_provider_config"

    KNOWN = {"sqlite_vec", "hindsight", "supermemory", "mem0", "none"}
    # Per-provider credential requirements: provider → (env_var, config_key).
    _CRED_REQS = {
        "hindsight": ("HINDSIGHT_API_KEY", "hindsight"),
        "supermemory": ("SUPERMEMORY_API_KEY", "supermemory"),
        "mem0": ("MEM0_API_KEY", "mem0"),
    }

    def run(self, ctx: DoctorContext) -> CheckResult:
        cfg = ctx.cfg or {}
        evo_section = (cfg.get("evolution") or {}).get("memory") or {}
        provider = evo_section.get("provider", "sqlite_vec")
        if provider not in self.KNOWN:
            return CheckResult(
                name=self.name, ok=False,
                detail=f"unknown provider: {provider!r}",
                advisory=f"set to one of: {', '.join(sorted(self.KNOWN))}",
            )
        if provider in self._CRED_REQS:
            env_var, sub_key = self._CRED_REQS[provider]
            sub = (evo_section.get(sub_key) or {})
            api_key = sub.get("api_key")
            import os as _os
            env_key = _os.environ.get(env_var)
            if not api_key and not env_key:
                return CheckResult(
                    name=self.name, ok=False,
                    detail=f"{provider} selected but no api_key configured",
                    advisory=(
                        f"set evolution.memory.{sub_key}.api_key in config "
                        f"OR {env_var} env var"
                    ),
                )
        return CheckResult(
            name=self.name, ok=True,
            detail=f"provider config: {provider}",
        )


class MemoryIndexerCheck(DoctorCheck):
    """B-42 + B-49: surface the state of the embedding pipeline AND
    actually ping the configured endpoint to verify it's reachable +
    the dim claim matches reality.

    States:
      * No embedding provider configured → OK "disabled" (indexer
        fall through, memory_search runs keyword)
      * Configured but unreachable / dim-mismatch → FAIL with concrete
        error
      * Configured + responds + dim matches → OK "ready: <vec_count>D
        from <model>"

    The probe is short — single ``embed(["ping"])`` call with a 5 s
    timeout. Skipped when only ``probe_daemon=False`` (offline doctor
    runs) or when no key/local-url is configured.
    """

    id = "memory_indexer"
    name = "memory_indexer"

    def run(self, ctx: DoctorContext) -> CheckResult:
        cfg = ctx.cfg or {}
        sec = (((cfg.get("evolution") or {}).get("memory") or {})
               .get("embedding") or {})
        import os as _os
        env_key = _os.environ.get("XMC_EMBEDDING_API_KEY")
        env_url = _os.environ.get("XMC_EMBEDDING_BASE_URL")

        # Empty config and no env override → indexer just disabled.
        if not sec and not env_key and not env_url:
            return CheckResult(
                name=self.name, ok=True,
                detail="indexer disabled (no embedding key configured)",
                advisory=(
                    "to enable semantic memory_search, set "
                    "evolution.memory.embedding.api_key in config OR "
                    "XMC_EMBEDDING_API_KEY env var"
                ),
            )
        api_key = sec.get("api_key")
        base_url = sec.get("base_url") or env_url or "https://api.openai.com/v1"
        is_local = any(s in base_url.lower() for s in (
            "://localhost", "://127.0.0.1", "://0.0.0.0", "://[::1]",
        ))
        if not api_key and not env_key and not is_local:
            return CheckResult(
                name=self.name, ok=False,
                detail="embedding section present but no api_key",
                advisory=(
                    "set evolution.memory.embedding.api_key OR "
                    "XMC_EMBEDDING_API_KEY env var (cloud endpoints "
                    "require auth; localhost endpoints don't)"
                ),
            )
        model = sec.get("model") or "text-embedding-3-small"
        dim = int(sec.get("dimensions") or 1536)

        # Skip the live probe when ``--no-probe`` was passed at CLI.
        # ``ctx.probe_daemon`` is the canonical flag the other check
        # bodies inspect.
        if not getattr(ctx, "probe_daemon", True):
            return CheckResult(
                name=self.name, ok=True,
                detail=f"indexer configured: model={model} dim={dim} (probe skipped)",
            )

        # Live probe.
        try:
            import asyncio as _aio
            from xmclaw.providers.memory.embedding import build_embedding_provider
            provider = build_embedding_provider(cfg)
            if provider is None:
                return CheckResult(
                    name=self.name, ok=False,
                    detail="provider failed to construct from config",
                    advisory="check evolution.memory.embedding shape",
                )
            vecs = _aio.run(_aio.wait_for(provider.embed(["ping"]), timeout=8.0))
            if not vecs or not vecs[0]:
                return CheckResult(
                    name=self.name, ok=False,
                    detail=f"embedding endpoint {base_url} returned empty result",
                    advisory=(
                        f"verify the model '{model}' is pulled / available "
                        "(for Ollama: ``ollama pull <model>``)"
                    ),
                )
            actual_dim = len(vecs[0])
            if actual_dim != dim:
                return CheckResult(
                    name=self.name, ok=False,
                    detail=(
                        f"dim mismatch: config says {dim}, model "
                        f"'{model}' returned {actual_dim}-D vectors"
                    ),
                    advisory=(
                        f"set evolution.memory.embedding.dimensions={actual_dim} "
                        "in config (mismatch will crash sqlite_vec at first put)"
                    ),
                )
            return CheckResult(
                name=self.name, ok=True,
                detail=(
                    f"indexer ready: {model} @ {base_url} → "
                    f"{actual_dim}D vectors"
                ),
            )
        except _aio.TimeoutError:
            return CheckResult(
                name=self.name, ok=False,
                detail=f"embedding endpoint {base_url} timed out (8s)",
                advisory="is the daemon / Ollama actually running?",
            )
        except Exception as exc:  # noqa: BLE001
            return CheckResult(
                name=self.name, ok=False,
                detail=f"embedding probe failed: {type(exc).__name__}: {exc}",
                advisory=(
                    f"endpoint {base_url} unreachable / refused; "
                    "verify it's running and the model exists"
                ),
            )


class PersonaProfileCheck(DoctorCheck):
    """B-56: surface the health of the active persona profile.

    Checks the 7 canonical files (SOUL/AGENTS/IDENTITY/USER/TOOLS/
    BOOTSTRAP/MEMORY) for existence + reasonable size. Flags:

      * Empty SOUL.md / IDENTITY.md (no character → agent has
        nothing to root its replies in)
      * MEMORY.md > 50 KB (long past the char-cap; Auto-Dream
        should have compacted by now)
      * Any canonical file > 200 KB (probably accidentally
        appended a huge blob)

    Doesn't fail on missing optional files (BOOTSTRAP is supposed
    to be deleted after first-run interview).
    """

    id = "persona_profile"
    name = "persona_profile"

    # Files we want to see + soft-required level.
    _REQUIRED = {"SOUL.md", "IDENTITY.md", "USER.md", "MEMORY.md"}
    _OPTIONAL = {"AGENTS.md", "TOOLS.md", "BOOTSTRAP.md"}
    # Per-file sane-size caps (bytes).
    _SOFT_CAP_BYTES = 50 * 1024
    _HARD_CAP_BYTES = 200 * 1024

    def run(self, ctx: DoctorContext) -> CheckResult:
        try:
            from xmclaw.daemon.factory import _resolve_persona_profile_dir
            pdir = _resolve_persona_profile_dir(ctx.cfg or {})
        except Exception as exc:  # noqa: BLE001
            return CheckResult(
                name=self.name, ok=False,
                detail=f"could not resolve persona dir: {exc}",
            )
        if not pdir.is_dir():
            # Treat as INFO not FAIL — doctor runs on fresh installs
            # too, and onboarding is a separate user-driven flow that
            # creates the dir + bootstrap files on first use.
            return CheckResult(
                name=self.name, ok=True,
                detail="persona dir not yet created (fresh install)",
                advisory="run 'xmclaw onboard' to bootstrap the profile",
            )
        problems: list[str] = []
        oversized: list[str] = []
        bloated: list[str] = []
        empty: list[str] = []
        for name in self._REQUIRED | self._OPTIONAL:
            p = pdir / name
            if not p.is_file():
                if name in self._REQUIRED and name != "BOOTSTRAP.md":
                    # BOOTSTRAP is deliberately deleted after onboard.
                    problems.append(f"{name} missing")
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if size == 0 and name in self._REQUIRED:
                empty.append(name)
            elif size > self._HARD_CAP_BYTES:
                oversized.append(f"{name}={size//1024}KB")
            elif size > self._SOFT_CAP_BYTES and name == "MEMORY.md":
                bloated.append(f"{name}={size//1024}KB")
        if problems or oversized:
            return CheckResult(
                name=self.name, ok=False,
                detail="; ".join(
                    (problems + ([f"oversized: {','.join(oversized)}"] if oversized else []))
                ),
                advisory=(
                    "missing required persona file → run 'xmclaw onboard'; "
                    "oversized → check for accidental binary/log dump"
                    if oversized else "missing required persona file → run 'xmclaw onboard'"
                ),
            )
        if bloated:
            return CheckResult(
                name=self.name, ok=True,
                detail=f"profile OK; bloated: {', '.join(bloated)}",
                advisory=(
                    "MEMORY.md is past 50KB — let Auto-Dream compact it "
                    "(POST /api/v2/memory/dream/run) or wait until the "
                    "next 03:00 cron"
                ),
            )
        if empty:
            return CheckResult(
                name=self.name, ok=True,
                detail=f"profile OK; empty: {', '.join(empty)}",
                advisory=(
                    f"{', '.join(empty)} are blank — fine for a fresh "
                    "install, but agent has less character grounding"
                ),
            )
        return CheckResult(
            name=self.name, ok=True,
            detail=f"profile OK at {pdir.name}",
        )


class DreamCronCheck(DoctorCheck):
    """B-56: surface the Auto-Dream cron state.

    Three states:
      * No LLM configured / dream disabled → OK "disabled"
      * Configured + last run failed → FAIL with the error text
      * Configured + healthy → OK "next 03:00 · last @ <when>"

    Live state lives on the daemon, not in config. We probe via the
    HTTP endpoint when ``probe_daemon=True``.
    """

    id = "dream_cron"
    name = "dream_cron"

    def run(self, ctx: DoctorContext) -> CheckResult:
        cfg = ctx.cfg or {}
        sec = ((cfg.get("evolution") or {}).get("dream") or {})
        enabled = sec.get("enabled", True)
        if not enabled:
            return CheckResult(
                name=self.name, ok=True,
                detail="dream cron disabled (evolution.dream.enabled=false)",
            )
        if not getattr(ctx, "probe_daemon", True):
            return CheckResult(
                name=self.name, ok=True,
                detail=f"dream config hour={sec.get('hour', 3):02d}:{sec.get('minute', 0):02d} (probe skipped)",
            )

        # Probe the daemon endpoint. Requires the daemon to be up.
        try:
            import urllib.request as _ur
            import urllib.error as _ue
            import json as _json
            url = "http://127.0.0.1:8765/api/v2/memory/dream/status"
            with _ur.urlopen(url, timeout=3.0) as resp:
                data = _json.loads(resp.read().decode("utf-8"))
        except _ue.URLError:
            # Daemon not running — different check (DaemonHealthCheck)
            # handles that. Don't double-fail here.
            return CheckResult(
                name=self.name, ok=True,
                detail="daemon not reachable (dream state unknown)",
            )
        except Exception as exc:  # noqa: BLE001
            return CheckResult(
                name=self.name, ok=True,
                detail=f"dream status probe failed: {exc}",
            )

        if not data.get("wired"):
            return CheckResult(
                name=self.name, ok=True,
                detail=f"dream not wired: {data.get('reason', 'unknown')}",
                advisory="configure an LLM to enable Auto-Dream",
            )
        last_result = data.get("last_result")
        last_at = data.get("last_run_at")
        hour = data.get("hour", 3)
        minute = data.get("minute", 0)
        when_label = (
            f"last @ {time.strftime('%Y-%m-%d %H:%M', time.localtime(last_at))}"
            if last_at else "never run yet"
        )
        if last_result and not last_result.get("ok"):
            return CheckResult(
                name=self.name, ok=False,
                detail=f"last dream failed: {last_result.get('error', '?')}",
                advisory="check Memory page or POST /api/v2/memory/dream/run to retry",
            )
        return CheckResult(
            name=self.name, ok=True,
            detail=(
                f"dream cron running, daily at {hour:02d}:{minute:02d}, {when_label}"
            ),
        )


class SkillRuntimeCheck(DoctorCheck):
    """Validate the ``runtime`` config section picks a known backend.

    ``runtime.backend`` drives which :class:`SkillRuntime` executes forked
    skills (see Epic #3 and :func:`xmclaw.daemon.factory.build_skill_runtime_from_config`).
    A typo here only explodes when the daemon tries to start, so this check
    runs the same builder the daemon uses and surfaces a clean error up front.

    States:
      * Section absent / ``backend`` unset   -> OK "local (default)"
      * Section present + known backend      -> OK "<backend>"
      * Section shape wrong / unknown backend -> FAIL with the ConfigError
        message verbatim — same wording the daemon would print.
    """

    id = "skill_runtime"
    name = "skill_runtime"

    def run(self, ctx: DoctorContext) -> CheckResult:
        cfg = ctx.cfg
        if cfg is None:
            # ConfigCheck already failed; don't double-fail.
            return CheckResult(
                name=self.name, ok=True,
                detail="skipped (config not loaded)",
            )
        from xmclaw.daemon.factory import (
            ConfigError,
            build_skill_runtime_from_config,
        )

        try:
            runtime = build_skill_runtime_from_config(cfg)
        except ConfigError as exc:
            return CheckResult(
                name=self.name, ok=False,
                detail=str(exc),
                advisory="fix daemon/config.json 'runtime' section "
                         "(see docs/CONFIG.md §Runtime)",
            )
        except Exception as exc:  # pragma: no cover — defensive
            return CheckResult(
                name=self.name, ok=False,
                detail=f"unexpected error building runtime: {exc!r}",
            )
        backend = "local"
        rt_section = cfg.get("runtime")
        if isinstance(rt_section, dict):
            cfg_backend = rt_section.get("backend")
            if isinstance(cfg_backend, str):
                backend = cfg_backend
        return CheckResult(
            name=self.name, ok=True,
            detail=f"{backend} ({type(runtime).__name__})",
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


class BackupsCheck(DoctorCheck):
    """Surface the backup inventory under ``~/.xmclaw/backups/``.

    Epic #20 sibling of the observability checks — not a hard failure
    gate (absence of a backup isn't broken config, it's a missed habit),
    but a visible hint that ``xmclaw backup create`` is available and
    how recently it was last used.

    States (all ``ok=True``, this is informational):
      * Backups dir missing or empty — detail ``"no backups yet"`` +
        advisory pointing at ``xmclaw backup create``.
      * One or more backups — detail ``"N backup(s), newest <age>"``;
        when the newest is older than :attr:`STALE_AFTER_DAYS`, the
        advisory nudges the user to run another create.

    Honors ``ctx.extras["backups_dir"]`` so tests can redirect; falls
    back to :func:`xmclaw.backup.store.default_backups_dir` (which
    itself honors ``XMC_BACKUPS_DIR``).
    """

    id = "backups"
    name = "backups"

    #: Age at which the newest backup is considered stale. 30 days is
    #: the "you should probably have run one this month" threshold —
    #: below daily-cadence expectations, above weekly-cadence noise.
    STALE_AFTER_DAYS = 30

    def _target(self, ctx: DoctorContext) -> Path:
        override = ctx.extras.get("backups_dir")
        if isinstance(override, (str, Path)):
            return Path(override)
        from xmclaw.backup.store import default_backups_dir

        return default_backups_dir()

    def run(self, ctx: DoctorContext) -> CheckResult:
        import time as _time

        from xmclaw.backup.store import list_backups

        root = self._target(ctx)
        entries = list_backups(root)
        if not entries:
            return CheckResult(
                name=self.name, ok=True,
                detail=f"no backups yet at {root}",
                advisory="run 'xmclaw backup create' to capture a snapshot",
            )
        # list_backups() returns ascending by created_ts; newest is last.
        newest = entries[-1]
        age_s = max(0.0, _time.time() - newest.manifest.created_ts)
        age_days = age_s / 86400.0
        age_fmt = _format_age(age_s)
        detail = (
            f"{len(entries)} backup(s) at {root}, newest '{newest.name}' "
            f"{age_fmt} old"
        )
        if age_days >= self.STALE_AFTER_DAYS:
            return CheckResult(
                name=self.name, ok=True,
                detail=detail,
                advisory=(
                    f"newest backup is {int(age_days)}d old — consider "
                    "'xmclaw backup create'"
                ),
            )
        return CheckResult(name=self.name, ok=True, detail=detail)


def _format_age(seconds: float) -> str:
    """Human-readable age. Keeps units coarse — we're in the "is this
    yesterday or last quarter" regime, not milliseconds."""
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


class SecretsCheck(DoctorCheck):
    """Surface the Epic #16 Phase 1 secrets inventory + common footguns.

    Three concerns the doctor should flag, ordered most-surprising first:

    1. **File mode on POSIX.** ``secrets.json`` must be 0600 — the write
       path sets it, but a copy / git checkout / manual edit can widen
       it. Wider-than-0600 is the only condition that flips ``ok=False``
       because it's a live leak: any other user on the box can ``cat``
       your API keys. :meth:`fix` auto-remediates with ``chmod 600``.

    2. **Env-var overrides.** When ``XMC_SECRET_FOO`` is exported the
       env value wins over the file — people lose hours chasing "why
       doesn't my secrets.json edit take effect". Surface it as an
       advisory (``ok=True``) so the info lands without crying wolf.

    3. **Baseline inventory.** "N secret(s) stored" so operators know
       the file is actually in use. Empty file = advisory suggesting
       ``xmclaw config set-secret``.

    Keyring content is deliberately not enumerated — there's no portable
    keyring-list API. Operators who want a full audit run their OS's
    credential-manager UI.
    """

    id = "secrets"
    name = "secrets"

    def _target(self, ctx: DoctorContext) -> Path:
        override = ctx.extras.get("secrets_path")
        if isinstance(override, (str, Path)):
            return Path(override)
        from xmclaw.utils.secrets import secrets_file_path

        return secrets_file_path()

    def run(self, ctx: DoctorContext) -> CheckResult:
        from xmclaw.utils.secrets import (
            iter_env_override_names,
            list_secret_names,
        )

        path = self._target(ctx)

        if not path.is_file():
            return CheckResult(
                name=self.name, ok=True,
                detail=f"no secrets file at {path}",
                advisory=(
                    "run 'xmclaw config set-secret <name>' to store an "
                    "API key outside of config.json"
                ),
            )

        names = list_secret_names()

        # Empty file carries no secrets → mode is not a live leak. This
        # matters because on Linux, pytest tmp dirs default to 0o644 and
        # `{path}.write_text("{}")` inherits it — so the first
        # ``set-secret`` tightens it, but a freshly touched empty file
        # shouldn't blow up CI. Emit an advisory nudging the operator to
        # actually store something, but keep ok=True.
        if not names:
            return CheckResult(
                name=self.name, ok=True,
                detail=f"secrets file at {path} is empty",
                advisory=(
                    "run 'xmclaw config set-secret <name>' to store an "
                    "API key outside of config.json"
                ),
            )

        # Mode check is POSIX-only. On Windows the file's ACLs are what
        # gate access; chmod is a no-op and any 0o??? bits we'd get back
        # are meaningless, so we skip the assertion entirely rather than
        # emit a false positive. If NT-ACL hardening ever lands, this is
        # where it hooks in.
        #
        # Runs *after* the empty-file branch: once there's real content,
        # 0o600 is enforced because a widened mode is a real leak.
        if os.name == "posix":
            mode = path.stat().st_mode & 0o777
            if mode != 0o600:
                return CheckResult(
                    name=self.name, ok=False,
                    detail=(
                        f"{path} has mode {oct(mode)} "
                        "(expected 0o600) — world/group readable"
                    ),
                    advisory=(
                        "run 'xmclaw doctor --fix' or "
                        f"'chmod 600 {path}' to tighten permissions"
                    ),
                    fix_available=True,
                )

        overrides = list(iter_env_override_names())
        detail = f"{len(names)} secret(s) at {path}"
        if overrides:
            preview = ", ".join(sorted(overrides)[:3])
            more = "" if len(overrides) <= 3 else f" (+{len(overrides) - 3} more)"
            return CheckResult(
                name=self.name, ok=True,
                detail=detail,
                advisory=(
                    f"env vars override {len(overrides)} entry(ies): "
                    f"{preview}{more} — unset them if you want the file "
                    "value to win"
                ),
            )
        return CheckResult(name=self.name, ok=True, detail=detail)

    def fix(self, ctx: DoctorContext) -> bool:
        """Tighten ``secrets.json`` to 0600 when it's wider.

        POSIX-only — on Windows we skip because chmod semantics don't
        apply. Returns True only when we actually narrowed the mode.
        """
        if os.name != "posix":
            return False
        path = self._target(ctx)
        if not path.is_file():
            return False
        current = path.stat().st_mode & 0o777
        if current == 0o600:
            return False
        try:
            os.chmod(path, 0o600)
        except OSError:
            return False
        return (path.stat().st_mode & 0o777) == 0o600


# B-78: per-section whitelist of immediate child keys that production
# code actually reads. Anything outside this map is flagged as a
# possible "ghost" field (legacy config from old templates, typos, or
# fields whose feature was deprecated and never removed). Sections
# mapped to None are permissive (mcp_servers' children are user-named
# servers; integrations.* are well-known but their inner shape is
# vendor-specific). Top-level keys starting with ``_`` are always
# allowed (user comments). Keep this list in sync when adding a new
# config section — `xmclaw doctor` will nudge users to clean up but
# a stale whitelist gives false positives, so the check is INFO-level
# (ok=True with advisory) rather than a hard fail.
_CONFIG_KNOWN_CHILD_KEYS: dict[str, set[str] | None] = {
    "llm": {
        "default_provider", "openai", "anthropic", "profiles",
        "compressor",
    },
    "evolution": {
        "enabled", "auto_apply", "interval_minutes",
        "daily_review_hour", "vfm_threshold", "max_genes_per_day",
        "auto_rollback", "dream", "memory",
        "pattern_thresholds", "tool_specific_thresholds",
    },
    "memory": {
        "enabled", "db_path", "embedding_dim", "ttl",
        "pinned_tags", "retention",
    },
    "tools": {"allowed_dirs", "enable_bash", "enable_web"},
    "runtime": {"backend"},
    "gateway": {"host", "port"},
    "security": {"prompt_injection", "guardians"},
    "backup": {"auto_daily", "interval_s", "keep", "name_prefix"},
    "mcp_servers": None,
    "integrations": None,
}

# Top-level sections that are entirely valid even though they're not in
# the schema map (e.g. someone wires a custom subsystem; we don't want
# to flag every new feature). Add here BEFORE adding to the map above
# during development.
_CONFIG_KNOWN_TOP_KEYS: set[str] = set(_CONFIG_KNOWN_CHILD_KEYS.keys())


class ConfigDeadFieldsCheck(DoctorCheck):
    """Flag config.json fields that no production code path reads.

    Motivation: real incident on 2026-04-29 — a user's config.json
    contained ``memory.vector_db_path`` / ``session_retention_days`` /
    ``max_context_tokens`` left over from a long-archived design
    document's example block. Those fields rendered as a "记忆与向量库"
    category in the Web UI's Config page (which is schema-driven from
    the live config dict) and the user reasonably assumed editing them
    would change behaviour — but no code anywhere read them. This check
    surfaces such ghosts during ``xmclaw doctor``.

    The whitelist of valid keys lives at ``_CONFIG_KNOWN_CHILD_KEYS``
    and ``_CONFIG_KNOWN_TOP_KEYS`` above. Underscored keys (``_comment``
    etc) are always allowed. Sections mapped to ``None`` (mcp_servers,
    integrations) are permissive — their child keys are user-named.

    Severity is INFO (``ok=True`` with advisory). A stale whitelist
    here would otherwise generate false-positive failures on every
    doctor run between a feature landing and someone updating this
    file. Operators see the advisory and decide.
    """

    id = "config_dead_fields"
    name = "config dead fields"

    def run(self, ctx: DoctorContext) -> CheckResult:
        cfg = ctx.cfg
        if not isinstance(cfg, dict):
            return CheckResult(
                name=self.name, ok=True,
                detail="no config loaded; nothing to scan",
            )
        ghosts: list[str] = []
        for top_key, top_val in cfg.items():
            if top_key.startswith("_"):
                continue
            if top_key not in _CONFIG_KNOWN_TOP_KEYS:
                ghosts.append(top_key)
                continue
            allowed = _CONFIG_KNOWN_CHILD_KEYS.get(top_key)
            if allowed is None or not isinstance(top_val, dict):
                continue  # permissive section / scalar — don't recurse
            for child_key in top_val.keys():
                if child_key.startswith("_"):
                    continue
                if child_key not in allowed:
                    ghosts.append(f"{top_key}.{child_key}")
        if not ghosts:
            return CheckResult(
                name=self.name, ok=True, detail="no unknown fields",
            )
        preview = ", ".join(ghosts[:5])
        more = "" if len(ghosts) <= 5 else f" (+{len(ghosts) - 5} more)"
        return CheckResult(
            name=self.name, ok=True,
            detail=f"{len(ghosts)} unknown field(s): {preview}{more}",
            advisory=(
                "these keys are not consumed by any production code path "
                "— likely stale from an older config template or a typo. "
                "Remove or rename. (Run 'xmclaw doctor --json' to see the "
                "full list.)"
            ),
        )


class EvolutionPathHygieneCheck(DoctorCheck):
    """Epic #24 Phase 1 — flag residual paths from torn-out subsystems.

    The user's load-bearing rule (2026-05-01): every user-state
    artifact has ONE canonical path. After Phase 1 deleted xm-auto-evo
    and the multi-root SKILL.md scanner, ``~/.xmclaw/auto_evo/``,
    ``~/.agents/skills/``, and ``~/.claude/skills/`` are no longer
    consulted by the agent at all. If they still exist on disk the
    user thinks they're "installed skills" but the agent literally
    can't see them — exactly the "install path != usage path" pain
    Epic #24 set out to remove. We surface this discrepancy here so
    the user knows to consolidate or delete.

    SkillRegistry's canonical path: ``~/.xmclaw/v2/skills/<id>/v<N>/``.
    """

    id: ClassVar[str] = "evolution_path_hygiene"
    name: ClassVar[str] = "evolution path hygiene"

    def run(self, ctx: DoctorContext) -> CheckResult:
        home = Path.home()
        suspects = [
            home / ".xmclaw" / "auto_evo",
            home / ".agents" / "skills",
            home / ".claude" / "skills",
        ]
        present = [p for p in suspects if p.exists()]
        if not present:
            return CheckResult(
                name=self.name, ok=True,
                detail="no residual SKILL.md trees from deleted subsystems",
            )
        joined = ", ".join(str(p) for p in present)
        return CheckResult(
            name=self.name, ok=False,
            detail=f"residual paths still on disk: {joined}",
            advisory=(
                "Epic #24 deleted the loaders that read these. The agent "
                "can no longer see SKILL.md files in them. Move anything "
                "you still want into the SkillRegistry (Phase 1 path: "
                "~/.xmclaw/v2/skills/<id>/v<N>/SKILL.md) or delete the "
                "residual trees."
            ),
        )


class EvolutionRuntimeCheck(DoctorCheck):
    """Epic #24 Phase 1 — verify the new evolution loop is wired.

    Two halves of the runtime contract:
      * AgentLoop calls HonestGrader after every tool — code-level
        check against ``xmclaw/daemon/agent_loop.py`` source.
      * EvolutionAgent observer is in app.state at boot — runtime
        check that requires the daemon to be running, otherwise we
        report "needs running daemon" rather than fail.
    """

    id: ClassVar[str] = "evolution_runtime"
    name: ClassVar[str] = "evolution runtime"

    def run(self, ctx: DoctorContext) -> CheckResult:  # noqa: ARG002
        # Source-level: assert HonestGrader is referenced in agent_loop.py.
        try:
            from xmclaw.daemon import agent_loop as _al
            src = Path(_al.__file__).read_text(encoding="utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            return CheckResult(
                name=self.name, ok=False,
                detail=f"cannot read agent_loop.py source: {exc}",
            )
        if "HonestGrader" not in src or "GRADER_VERDICT" not in src:
            return CheckResult(
                name=self.name, ok=False,
                detail="HonestGrader not wired into AgentLoop",
                advisory=(
                    "Expected `self._grader = HonestGrader()` and a "
                    "`publish(EventType.GRADER_VERDICT, ...)` call after "
                    "each TOOL_INVOCATION_FINISHED. Reapply Epic #24 "
                    "Phase 1 changes if missing."
                ),
            )
        return CheckResult(
            name=self.name, ok=True,
            detail="HonestGrader wired + GRADER_VERDICT published per tool",
        )


class EvolutionPipelineCheck(DoctorCheck):
    """Epic #24 Phase 4.3 — the four observers + LLM extractor wiring.

    Source-level verification: ``xmclaw/daemon/app.py`` lifespan must
    construct + start each of the four observers Phase 1-3 introduced
    (EvolutionAgent / JournalWriter / ProfileExtractor /
    SkillDreamCycle), AND must wire the LLM-backed extractor when an
    LLM is available (Phase 3.5 ``build_skill_extractor`` /
    ``build_profile_extractor``).

    Failure here means a regression unwound part of the chain — e.g.
    someone deleted the ProfileExtractor block thinking it was dead
    code. Reapply Epic #24 Phase 2 / Phase 3.5 changes.

    This is a *static* check; runtime health (is the task actually
    running?) needs the daemon up. ``DaemonHealthCheck`` covers that.
    """

    id: ClassVar[str] = "evolution_pipeline"
    name: ClassVar[str] = "evolution pipeline wiring"

    REQUIRED_TOKENS: ClassVar[tuple[str, ...]] = (
        "EvolutionAgent",       # Phase 1
        "JournalWriter",        # Phase 2.1
        "ProfileExtractor",     # Phase 2.2
        "SkillDreamCycle",      # Phase 3.2
        "build_skill_extractor",   # Phase 3.5 — LLM extractor wired in
        "build_profile_extractor",
    )

    def run(self, ctx: DoctorContext) -> CheckResult:  # noqa: ARG002
        try:
            from xmclaw.daemon import app as _app_mod
            src = Path(_app_mod.__file__).read_text(encoding="utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            return CheckResult(
                name=self.name, ok=False,
                detail=f"cannot read daemon/app.py source: {exc}",
            )
        missing = [tok for tok in self.REQUIRED_TOKENS if tok not in src]
        if missing:
            return CheckResult(
                name=self.name, ok=False,
                detail=f"daemon/app.py missing wiring for: {', '.join(missing)}",
                advisory=(
                    "Each of these tokens names an observer / LLM-extractor "
                    "factory the lifespan should reference. Restore the "
                    "Epic #24 wiring (search docs/DEV_ROADMAP.md for the "
                    "Phase that introduced the missing token)."
                ),
            )
        return CheckResult(
            name=self.name, ok=True,
            detail="4 observers + LLM-backed extractor factories all wired",
        )


def build_default_registry() -> DoctorRegistry:
    """Return a registry populated with the built-in checks.

    Order matters: ConfigCheck must run first so subsequent checks
    can read ``ctx.cfg``.
    """
    reg = DoctorRegistry()
    reg.register(ConfigCheck())
    reg.register(ConfigDeadFieldsCheck())
    reg.register(LLMCheck())
    reg.register(ToolsCheck())
    reg.register(WorkspaceCheck())
    reg.register(PairingCheck())
    reg.register(PortCheck())
    reg.register(EventsDbCheck())
    reg.register(MemoryDbCheck())
    reg.register(MemoryProviderCheck())
    reg.register(MemoryProviderConfigCheck())
    reg.register(MemoryIndexerCheck())
    reg.register(PersonaProfileCheck())
    reg.register(DreamCronCheck())
    reg.register(SkillRuntimeCheck())
    reg.register(ConnectivityCheck())
    reg.register(RoadmapLintCheck())
    reg.register(StalePidCheck())
    reg.register(DaemonHealthCheck())
    reg.register(BackupsCheck())
    reg.register(SecretsCheck())
    reg.register(EvolutionPathHygieneCheck())  # Epic #24 Phase 1
    reg.register(EvolutionRuntimeCheck())      # Epic #24 Phase 1
    reg.register(EvolutionPipelineCheck())     # Epic #24 Phase 4.3
    return reg
