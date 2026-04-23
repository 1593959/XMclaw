# xmclaw/backup/AGENTS.md — Backup/restore (Epic #20)

## 1. 职责（Responsibility）

Single source of truth for packaging the ``~/.xmclaw/`` workspace
into a portable, verifiable tar.gz + manifest pair, and for
restoring it back. Does **not** own daemon lifecycle — the caller
(CLI, operator) must stop the daemon before restore and restart
after. Phase 2 of Epic #20 adds ``daemon/reloader.py`` to close that
loop; until then, this directory is filesystem-only.

## 2. 依赖规则（Dependency rules）

- ✅ MAY import: Python stdlib (``tarfile``, ``hashlib``, ``shutil``,
  ``importlib.metadata``), ``xmclaw.utils.paths``.
- ❌ MUST NOT import: ``xmclaw.core.*``, ``xmclaw.providers.*``,
  ``xmclaw.daemon.*``. Restore must be runnable from a rescue
  environment where the daemon and its providers may be broken —
  pulling in core.bus for the sake of a single ``make_event`` call
  would defeat that.

Rationale: if restore depends on a working core/providers tree, a
corrupted install can't be repaired by restoring a known-good
backup, which defeats the whole feature.

## 3. 测试入口（How to test changes here）

- Unit: `tests/unit/test_v2_backup.py` (create/restore round-trip,
  checksum verification, manifest schema, exclude rules, tar-slip
  defense).
- Smart-gate lane: `backup` in `scripts/test_lanes.yaml`.
- Manual:
  ```bash
  XMC_DATA_DIR=/tmp/xmc-test python -c "
  from pathlib import Path
  from xmclaw.backup import create_backup, restore_backup
  Path('/tmp/xmc-test').mkdir(exist_ok=True)
  (Path('/tmp/xmc-test/hello.txt')).write_text('hi')
  m = create_backup(Path('/tmp/xmc-test'), 'smoke')
  print(m.archive_sha256)
  "
  ```

## 4. 禁止事项（Hard no's）

- ❌ **Never extract without path-validating every member.** ``tarfile.
  extractall`` with untrusted input is tar-slip territory; we control
  the archives today but an attacker who plants a malicious backup
  under ``~/.xmclaw/backups/`` must not be able to write outside the
  target. ``_safe_extract`` resolves every member and checks it's
  inside the target — do not remove that check.
- ❌ **Never skip the sha256 verify.** The whole point of the
  manifest is integrity. An "optimization" that reads the tar once
  is wrong — we re-hash before extract so a flipped bit becomes a
  RestoreError, not a silently-broken workspace.
- ❌ **Never delete the old tree before the new one is in place.**
  The swap order is: (a) rename old aside, (b) rename staging to
  target, (c) if (b) fails, rename old back. Reordering loses data
  on a failed extract.
- ❌ **Never put daemon imports here.** See §2 rationale.

## 5. 关键文件（Key files / entry points）

- `manifest.py:19` — `Manifest` dataclass + `MANIFEST_SCHEMA_VERSION`.
  Read this first before changing the on-disk format.
- `create.py:62` — `create_backup(source, name)` entry point.
  `DEFAULT_EXCLUDED` lives here; add to it rather than inventing a
  per-caller exclude list.
- `restore.py:37` — `restore_backup(name, target)` entry point.
  `_safe_extract` is the tar-slip guard; `_verify_checksum` is the
  integrity gate.
- `store.py:44` — `list_backups()` + `BackupEntry`. Tolerant of
  malformed backup dirs by design.
- `__init__.py` — frozen public surface. Tests pin it.
