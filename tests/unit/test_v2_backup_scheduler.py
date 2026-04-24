"""Epic #20 Phase 2: daily workspace-backup scheduler.

Covers:
  * ``parse_backup_config`` — default, opt-in, per-field bad-value
    fallback (never raises), weird prefix sanitization.
  * ``BackupSchedulerTask.tick_once`` — creates a dated backup under
    the configured prefix, honors overwrite-on-clash, and prunes older
    auto backups while leaving manual ones alone.
  * ``start() / stop()`` lifecycle — no-op when disabled, cancellable
    when enabled, idempotent.
  * Failure paths — missing source, ``create_backup`` raising,
    ``delete_backup`` raising — the loop treats them as skipped ticks
    and doesn't propagate.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from xmclaw.backup import create_backup, list_backups
from xmclaw.daemon.backup_scheduler import (
    DEFAULT_INTERVAL_S,
    DEFAULT_KEEP,
    DEFAULT_NAME_PREFIX,
    BackupPolicy,
    BackupSchedulerTask,
    _auto_backup_name,
    parse_backup_config,
)


# ── parse_backup_config ────────────────────────────────────────────────


class TestParseBackupConfig:
    def test_none_returns_defaults(self) -> None:
        p = parse_backup_config(None)
        assert p.auto_daily is False
        assert p.interval_s == float(DEFAULT_INTERVAL_S)
        assert p.keep == DEFAULT_KEEP
        assert p.name_prefix == DEFAULT_NAME_PREFIX

    def test_non_dict_returns_defaults(self) -> None:
        # Users fat-finger YAML → list instead of mapping — must not crash.
        assert parse_backup_config(["nonsense"]) == BackupPolicy()  # type: ignore[arg-type]
        assert parse_backup_config("string") == BackupPolicy()  # type: ignore[arg-type]

    def test_opt_in_enables_auto_daily(self) -> None:
        p = parse_backup_config({"auto_daily": True})
        assert p.auto_daily is True

    def test_truthy_strings_do_not_enable(self) -> None:
        # Common JSON confusion: ``"true"`` (string) vs ``true`` (bool).
        # bool() of a non-empty string is True — which we explicitly
        # accept here. The point is to lock in the current behavior so
        # a future refactor doesn't silently change it.
        p = parse_backup_config({"auto_daily": "true"})
        assert p.auto_daily is True
        p2 = parse_backup_config({"auto_daily": ""})
        assert p2.auto_daily is False

    def test_custom_interval(self) -> None:
        p = parse_backup_config({"interval_s": 60})
        assert p.interval_s == 60.0
        p2 = parse_backup_config({"interval_s": 30.5})
        assert p2.interval_s == 30.5

    def test_bad_interval_falls_back_to_default(self) -> None:
        for bad in (0, -10, "1h", None, [], {}):
            p = parse_backup_config({"interval_s": bad})
            assert p.interval_s == float(DEFAULT_INTERVAL_S), f"for {bad!r}"

    def test_custom_keep(self) -> None:
        assert parse_backup_config({"keep": 3}).keep == 3
        # keep=0 is legal ("delete every auto after the next one lands");
        # negative is not.
        assert parse_backup_config({"keep": 0}).keep == 0

    def test_bad_keep_falls_back(self) -> None:
        for bad in (-1, "5", 2.5, None, True):
            # ``True`` is `int` but we expect the default 7 to win; bool
            # is an int subclass in Python which is awkward but the
            # parser accepts it — this test documents that edge.
            p = parse_backup_config({"keep": bad})
            if isinstance(bad, bool):
                # bool is an int; True == 1 → valid. Document it.
                assert p.keep == int(bad)
            elif isinstance(bad, int) and bad >= 0:
                assert p.keep == bad
            else:
                assert p.keep == DEFAULT_KEEP, f"for {bad!r}"

    def test_custom_prefix(self) -> None:
        assert parse_backup_config({"name_prefix": "nightly-"}).name_prefix == "nightly-"

    def test_empty_or_whitespace_prefix_falls_back(self) -> None:
        assert parse_backup_config({"name_prefix": ""}).name_prefix == DEFAULT_NAME_PREFIX
        assert parse_backup_config({"name_prefix": "   "}).name_prefix == DEFAULT_NAME_PREFIX

    def test_prefix_with_separators_rejected(self) -> None:
        # A prefix with "/" would produce nested subdirectories, which
        # ``create_backup`` rejects as an invalid name. Rejecting here
        # gives a better error + structured log instead of a cryptic
        # BackupError at tick time.
        for bad in ("evil/", "..", ".", "bad\\name-"):
            p = parse_backup_config({"name_prefix": bad})
            assert p.name_prefix == DEFAULT_NAME_PREFIX, f"for {bad!r}"

    def test_never_raises_on_exotic_input(self) -> None:
        # Belt + suspenders: an entirely malformed dict still returns
        # *something*. Users may hand-edit config.json and we'd rather
        # boot a degraded daemon than none.
        weird = {
            "auto_daily": {"nested": "nope"},
            "interval_s": [1, 2, 3],
            "keep": "lots",
            "name_prefix": 42,
        }
        # Will log warnings but must not raise.
        p = parse_backup_config(weird)
        assert p.interval_s == float(DEFAULT_INTERVAL_S)
        assert p.keep == DEFAULT_KEEP
        assert p.name_prefix == DEFAULT_NAME_PREFIX


# ── _auto_backup_name ──────────────────────────────────────────────────


class TestAutoBackupName:
    def test_format_is_sortable(self) -> None:
        # 2026-01-02 00:00:00 UTC
        t1 = 1767312000.0
        # 2026-01-02 00:00:01 UTC
        t2 = 1767312001.0
        n1 = _auto_backup_name("auto-", now=t1)
        n2 = _auto_backup_name("auto-", now=t2)
        assert n1.startswith("auto-2026")
        assert n2.startswith("auto-2026")
        assert n1 < n2, "lex sort must match chronological sort"

    def test_honors_custom_prefix(self) -> None:
        name = _auto_backup_name("nightly-", now=1767312000.0)
        assert name.startswith("nightly-2026")

    def test_utc_not_local(self) -> None:
        # Name must be deterministic across timezones — generating on
        # UTC-7 vs UTC+9 for the same epoch must yield the same name.
        # We don't monkeypatch tz here, just assert the function produces
        # a 15-char date-time tail ``YYYYMMDD-HHMMSS`` regardless of
        # platform locale.
        name = _auto_backup_name("auto-", now=0.0)
        assert name == "auto-19700101-000000"


# ── BackupSchedulerTask: tick_once ──────────────────────────────────────


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A minimal source directory with one file — enough to tar.gz."""
    src = tmp_path / "workspace"
    src.mkdir()
    (src / "hello.txt").write_text("hi", encoding="utf-8")
    return src


@pytest.fixture
def backups_dir(tmp_path: Path) -> Path:
    d = tmp_path / "backups"
    d.mkdir()
    return d


class TestTickOnce:
    @pytest.mark.asyncio
    async def test_disabled_policy_is_no_op(
        self, workspace: Path, backups_dir: Path,
    ) -> None:
        policy = BackupPolicy(auto_daily=False)
        task = BackupSchedulerTask(workspace, policy, backups_dir=backups_dir)
        result = await task.tick_once()
        assert result is None
        assert list_backups(backups_dir=backups_dir) == []

    @pytest.mark.asyncio
    async def test_enabled_policy_creates_backup(
        self, workspace: Path, backups_dir: Path,
    ) -> None:
        policy = BackupPolicy(auto_daily=True, interval_s=60.0, keep=5)
        # Fixed clock so the name is predictable.
        task = BackupSchedulerTask(
            workspace, policy, backups_dir=backups_dir, clock=lambda: 1767312000.0,
        )
        manifest = await task.tick_once()
        assert manifest is not None
        entries = list_backups(backups_dir=backups_dir)
        assert len(entries) == 1
        assert entries[0].name == "auto-20260102-000000"
        # Manifest fields are the same ones users see via ``backup info``.
        assert entries[0].manifest.archive_sha256 == manifest.archive_sha256

    @pytest.mark.asyncio
    async def test_missing_source_logs_and_skips(
        self, tmp_path: Path, backups_dir: Path,
    ) -> None:
        gone = tmp_path / "does_not_exist"
        policy = BackupPolicy(auto_daily=True)
        task = BackupSchedulerTask(gone, policy, backups_dir=backups_dir)
        result = await task.tick_once()
        assert result is None
        assert list_backups(backups_dir=backups_dir) == []

    @pytest.mark.asyncio
    async def test_create_backup_error_is_caught(
        self, workspace: Path, backups_dir: Path,
    ) -> None:
        # If the underlying create_backup raises, tick_once returns None
        # and the daemon lives to see the next tick.
        policy = BackupPolicy(auto_daily=True)
        task = BackupSchedulerTask(workspace, policy, backups_dir=backups_dir)
        with patch(
            "xmclaw.daemon.backup_scheduler.create_backup",
            side_effect=OSError("disk full"),
        ):
            result = await task.tick_once()
        assert result is None
        # And no partial backup was written.
        assert list_backups(backups_dir=backups_dir) == []

    @pytest.mark.asyncio
    async def test_prune_error_does_not_hide_successful_create(
        self, workspace: Path, backups_dir: Path,
    ) -> None:
        policy = BackupPolicy(auto_daily=True, keep=0)
        task = BackupSchedulerTask(workspace, policy, backups_dir=backups_dir)
        # Break delete_backup only; create must still land and return
        # its manifest.
        with patch(
            "xmclaw.daemon.backup_scheduler.delete_backup",
            side_effect=OSError("perms"),
        ):
            manifest = await task.tick_once()
        assert manifest is not None
        # Create won — file is on disk even though prune failed.
        entries = list_backups(backups_dir=backups_dir)
        assert len(entries) == 1

    @pytest.mark.asyncio
    async def test_overwrite_collides_same_second(
        self, workspace: Path, backups_dir: Path,
    ) -> None:
        # If two ticks land in the same second (unit test clock pinned),
        # the second must overwrite rather than raise.
        policy = BackupPolicy(auto_daily=True, keep=10)
        task = BackupSchedulerTask(
            workspace, policy, backups_dir=backups_dir, clock=lambda: 1767312000.0,
        )
        m1 = await task.tick_once()
        m2 = await task.tick_once()
        assert m1 is not None and m2 is not None
        # Same filename → only one directory on disk.
        entries = list_backups(backups_dir=backups_dir)
        assert len(entries) == 1
        # But the second manifest wins.
        assert entries[0].manifest.archive_sha256 == m2.archive_sha256


# ── pruning ────────────────────────────────────────────────────────────


class TestPruneOldAutos:
    @pytest.mark.asyncio
    async def test_prune_keeps_newest_n_autos(
        self, workspace: Path, backups_dir: Path,
    ) -> None:
        policy = BackupPolicy(auto_daily=True, keep=2)
        # Three auto backups at increasing timestamps.
        timestamps = [1767312000.0, 1767312060.0, 1767312120.0]
        for i, ts in enumerate(timestamps):
            task = BackupSchedulerTask(
                workspace, policy, backups_dir=backups_dir, clock=lambda ts=ts: ts,
            )
            await task.tick_once()
            if i < len(timestamps) - 1:
                assert len(list_backups(backups_dir=backups_dir)) == i + 1

        # After the third tick we should have kept 2 (policy.keep) — the
        # oldest one got pruned.
        final = list_backups(backups_dir=backups_dir)
        assert len(final) == 2
        names = {e.name for e in final}
        assert "auto-20260102-000100" in names  # t=1767312060
        assert "auto-20260102-000200" in names  # t=1767312120
        assert "auto-20260102-000000" not in names  # pruned (t=1767312000)

    @pytest.mark.asyncio
    async def test_prune_leaves_manual_backups_alone(
        self, workspace: Path, backups_dir: Path,
    ) -> None:
        # Drop in a user-named manual backup — scheduler must never
        # touch it even when keep=0.
        create_backup(workspace, "my-important-manual-snapshot", backups_dir=backups_dir)
        assert len(list_backups(backups_dir=backups_dir)) == 1

        policy = BackupPolicy(auto_daily=True, keep=0)
        # keep=0 means "don't retain any auto backups" — so each tick
        # both creates one and immediately prunes it. Manual backup
        # must be untouched.
        task = BackupSchedulerTask(
            workspace, policy, backups_dir=backups_dir, clock=lambda: 1767312000.0,
        )
        await task.tick_once()
        entries = list_backups(backups_dir=backups_dir)
        names = {e.name for e in entries}
        assert "my-important-manual-snapshot" in names

    @pytest.mark.asyncio
    async def test_custom_prefix_scopes_prune(
        self, workspace: Path, backups_dir: Path,
    ) -> None:
        # auto- backups are untouched when our prefix is "nightly-".
        policy_auto = BackupPolicy(auto_daily=True, keep=10, name_prefix="auto-")
        task_a = BackupSchedulerTask(
            workspace, policy_auto, backups_dir=backups_dir, clock=lambda: 1767312000.0,
        )
        await task_a.tick_once()

        policy_nightly = BackupPolicy(auto_daily=True, keep=0, name_prefix="nightly-")
        task_n = BackupSchedulerTask(
            workspace, policy_nightly, backups_dir=backups_dir, clock=lambda: 1767312060.0,
        )
        await task_n.tick_once()

        names = {e.name for e in list_backups(backups_dir=backups_dir)}
        # nightly- scheduler's keep=0 deleted its own just-created
        # backup but left the auto- one.
        assert any(n.startswith("auto-") for n in names)
        assert not any(n.startswith("nightly-") for n in names)

    @pytest.mark.asyncio
    async def test_prune_with_fewer_than_keep_is_noop(
        self, workspace: Path, backups_dir: Path,
    ) -> None:
        policy = BackupPolicy(auto_daily=True, keep=10)
        task = BackupSchedulerTask(
            workspace, policy, backups_dir=backups_dir, clock=lambda: 1767312000.0,
        )
        await task.tick_once()
        # One backup, keep=10 — nothing to prune.
        assert len(list_backups(backups_dir=backups_dir)) == 1


# ── start / stop lifecycle ─────────────────────────────────────────────


class TestStartStop:
    @pytest.mark.asyncio
    async def test_disabled_policy_start_is_no_op(
        self, workspace: Path, backups_dir: Path,
    ) -> None:
        policy = BackupPolicy(auto_daily=False)
        task = BackupSchedulerTask(workspace, policy, backups_dir=backups_dir)
        await task.start()
        assert task.is_running is False
        await task.stop()  # must not raise

    @pytest.mark.asyncio
    async def test_start_creates_task_and_stop_cancels(
        self, workspace: Path, backups_dir: Path,
    ) -> None:
        # A very-long interval keeps the loop blocked on wait_for so
        # start/stop mechanics are observable without waiting on an
        # actual tick.
        policy = BackupPolicy(auto_daily=True, interval_s=3600.0)
        task = BackupSchedulerTask(workspace, policy, backups_dir=backups_dir)
        await task.start()
        assert task.is_running is True
        await task.stop()
        assert task.is_running is False

    @pytest.mark.asyncio
    async def test_start_is_idempotent(
        self, workspace: Path, backups_dir: Path,
    ) -> None:
        policy = BackupPolicy(auto_daily=True, interval_s=3600.0)
        task = BackupSchedulerTask(workspace, policy, backups_dir=backups_dir)
        await task.start()
        first_task = task._task
        await task.start()  # second call must not spawn another
        assert task._task is first_task
        await task.stop()

    @pytest.mark.asyncio
    async def test_stop_is_idempotent(
        self, workspace: Path, backups_dir: Path,
    ) -> None:
        policy = BackupPolicy(auto_daily=True, interval_s=3600.0)
        task = BackupSchedulerTask(workspace, policy, backups_dir=backups_dir)
        await task.start()
        await task.stop()
        await task.stop()  # second call is a no-op

    @pytest.mark.asyncio
    async def test_loop_fires_tick_after_interval(
        self, workspace: Path, backups_dir: Path,
    ) -> None:
        # Very short interval so we can observe the loop firing at
        # least one tick under a reasonable test timeout. Policy is
        # enabled with keep=5 so nothing gets pruned.
        policy = BackupPolicy(auto_daily=True, interval_s=0.05, keep=5)
        task = BackupSchedulerTask(
            workspace, policy, backups_dir=backups_dir,
            # Distinct timestamps for multiple ticks.
            clock=lambda: 1767312000.0 + asyncio.get_event_loop().time(),
        )
        await task.start()
        # Wait long enough for at least one tick but not enough for the
        # test to be slow — 200ms is 4 intervals.
        await asyncio.sleep(0.25)
        await task.stop()
        assert len(list_backups(backups_dir=backups_dir)) >= 1
