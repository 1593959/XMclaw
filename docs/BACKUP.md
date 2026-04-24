# BACKUP.md — Backup & Restore

User guide for the `xmclaw backup` CLI family. Phase 1 ships eight
subcommands that cover the read-your-own-rescue-plan loop: create,
inspect, verify, prune, restore. Automatic daily backups and zero-
downtime reload land in Phase 2 — see §8.

> Status: Phase 1 — `xmclaw backup {create,list,info,verify,delete,prune,restore}`.
> Epic #20 roadmap: [DEV_ROADMAP.md §Epic #20](DEV_ROADMAP.md).

## 1. Why

Your `~/.xmclaw/` directory is the whole agent: conversation history,
promoted skills, memory DB, pairing token, config. A corrupted SQLite
file or an overeager `rm -rf` is hours of agent-training gone. Phase 1
makes the cost of a backup (`create`) and the cost of proving the
backup is good (`verify`) both one-liners, so there's no excuse to
skip them.

The design intentionally avoids anything exotic:

- **Plain tar.gz + manifest.json.** No custom binary format. If
  `xmclaw` is uninstalled, you can still `tar xzf` your data.
- **Atomic directory swap on restore.** The previous workspace moves
  to `<target>.prev-<ts>` unless you pass `--no-keep-previous`, so a
  bad restore is reversible with a `mv`.
- **Content-addressed integrity.** Every manifest carries the sha256
  of the archive; `verify` re-hashes without extracting, fast enough
  for pre-restore sanity checks or long-running bit-rot scans.

## 2. What gets backed up

`xmclaw backup create` archives everything under the source directory
(`--source`, default `$XMC_DATA_DIR` or `~/.xmclaw`) **except**:

| Pattern | Why it's excluded |
|---|---|
| `logs`, `logs/*`, `*/logs/*` | Regenerated on next daemon start |
| `*/__pycache__`, `*/__pycache__/*` | Python bytecode, not data |
| `daemon.pid`, `daemon.meta`, `daemon.log`, `*.pid`, `*.tmp` | Runtime artefacts — a restored daemon picks a new pid |

The full pattern list lives in `xmclaw/backup/create.py::DEFAULT_EXCLUDED`.
Override with `create_backup(..., excluded=(...))` from Python; the CLI
uses defaults only.

Everything else — `events.db`, `memory.db`, `secrets.json`, the
`skills/` registry, `pairing_token.txt`, config — is included
verbatim.

## 3. Quick start

```bash
# Take a named snapshot right now.
xmclaw backup create milestone-pre-refactor

# See what you have.
xmclaw backup list

# Confirm the archive still matches its manifest (read-only, no extract).
xmclaw backup verify milestone-pre-refactor

# Put it back. Stop the daemon first so nothing writes during extract.
xmclaw stop
xmclaw backup restore milestone-pre-refactor
xmclaw start
```

That's the whole loop. The rest of this doc is reference.

## 4. Command reference

All commands accept `--dest <path>` to point at a non-default backups
directory (defaults to `<source>/backups` for `create` / `~/.xmclaw/backups`
for everything else). All commands that can produce a structured shape
accept `--json` — use these in CI and monitoring instead of parsing
text.

### `xmclaw backup create [name]`

Make a new backup. If `name` is omitted it defaults to
`auto-YYYY-MM-DD-HHMMSS` (UTC).

| Flag | Default | Notes |
|---|---|---|
| `--source <path>` | `$XMC_DATA_DIR` or `~/.xmclaw` | What to archive. |
| `--dest <path>` | `<source>/backups` | Where the backup goes. |
| `--overwrite` | off | Required when reusing an existing name. |

Output (text mode):

```
  [ok]  milestone-pre-refactor: 142 file(s), 9324871 bytes, sha256=3f2a1b9c5e7d...
```

Exit codes: `0` success; `1` any `BackupError` (source missing,
name collision without `--overwrite`, disk full, …).

### `xmclaw backup list [--json]`

Columnar text by default, stable JSON array with `--json` (same shape
per element as `backup info --json`). Empty dir prints
`no backups found.` in text mode and `[]` in JSON mode — pipelines
can detect the empty case by array length, no string grep needed.

### `xmclaw backup info <name> [--show-excluded] [--json]`

Pretty-prints a single backup's `manifest.json`. Cheaper than
`verify`: reads the manifest only, does not re-hash.

- `--show-excluded` appends the glob list (hidden by default — it's
  13 items and usually noise).
- `--json` emits the full manifest dict **including `excluded`**
  regardless of `--show-excluded` (scripts want determinism).

### `xmclaw backup verify <name> [--json]`

Re-hashes the archive and compares against the manifest. Does **not**
extract. Use before a risky restore, after moving backups to slower
storage, or on a schedule to catch bit-rot.

Failure modes surfaced as `RestoreError` (exit 1):

- Missing backup directory, missing archive, missing manifest
- Schema-version newer than supported (future-proofing — upgrade
  `xmclaw` and retry)
- Archive bytes or sha256 drifted from the manifest

`--json` output:

```json
# pass
{"ok": true, "name": "milestone-pre-refactor", "entries": 142, "archive_bytes": 9324871, "archive_sha256": "3f2a1b9c5e7d..."}
# fail
{"ok": false, "name": "milestone-pre-refactor", "error": "archive sha256 drift: stored 3f2a... got 9e41..."}
```

Exit code still tracks success — `--json` is a shape switch, not a
failure-suppression mode. `xmclaw backup verify foo --json | jq .ok`
and `xmclaw backup verify foo --json || page-oncall` both work.

### `xmclaw backup delete <name> [--yes]`

Remove a single backup (archive + manifest + enclosing directory).
Prompts by default; `--yes` / `-y` skips. Refuses names containing
path separators or that escape the backups root (symlink guard).

### `xmclaw backup prune [--keep N] [--yes]`

Keep only the `N` newest backups (by `created_ts`), delete the rest.
Default `--keep 5`. Dry-run first — without `--yes`, `prune` echoes
the list it *would* remove and requires interactive confirmation.
Exit 2 on invalid `--keep` (e.g. negative).

### `xmclaw backup restore <name>`

Extract the archive back into the target workspace. Four-step flow:

1. Load + validate the manifest (schema version check, sha256
   verify over the archive bytes).
2. Extract into `<target>.restore-staging/` (new dir, so a crash
   mid-extract leaves the live workspace untouched).
3. If `<target>` exists and `--keep-previous` (default on), rename
   `<target>` → `<target>.prev-<UTC-timestamp>`. Rollback is a
   single `mv` away.
4. Atomically rename staging → `<target>`.

| Flag | Default | Notes |
|---|---|---|
| `--target <path>` | `$XMC_DATA_DIR` or `~/.xmclaw` | Where to restore. |
| `--dest <path>` | `~/.xmclaw/backups` | Where the backup lives. |
| `--keep-previous` / `--no-keep-previous` | on | Preserve the old target as `.prev-<ts>`. |

**Phase 1 does not stop or restart the daemon.** Run `xmclaw stop`
before restore and `xmclaw start` after, or the running daemon will
be writing to a directory you just swapped out from under it. Phase 2
will wire a `daemon/reloader.py` draining protocol and make `restore`
hot-reloadable.

Tar-slip defence is in `_safe_extract` — every archive member is
resolved against the target root and rejected if it escapes.

## 5. Manifest schema (v1)

Frozen shape in `xmclaw/backup/manifest.py`:

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | `int` | Always `1` today. `restore` / `verify` reject backups with a newer value. |
| `name` | `str` | Backup name (matches directory name under `backups/`). |
| `created_ts` | `float` | Unix timestamp (seconds), UTC. |
| `xmclaw_version` | `str` | Version that produced the archive. |
| `source_dir` | `str` | Absolute path of the workspace that was archived. |
| `archive_sha256` | `str` | Full sha256 hex of `archive.tar.gz`. |
| `archive_bytes` | `int` | Byte size of the archive. |
| `excluded` | `list[str]` | Glob patterns skipped (see §2). |
| `entries` | `int` | Number of files included. |

Readers are permissive (unknown fields ignored) but writers are
strict — add a field → bump `MANIFEST_SCHEMA_VERSION` + gate on
it in `restore` / `verify`.

## 6. Scriptability

Every read-only command has a `--json` mode. `list` and `info` share
the same per-entry shape — `xmclaw backup list --json | jq '.[0]'`
yields the same dict as `xmclaw backup info <first-name> --json`. Use
this to build health dashboards:

```bash
# Flag any backup older than 7 days.
xmclaw backup list --json | jq -r \
  '.[] | select((now - .created_ts) > 7*86400) | .name'

# Abort a deploy if verify doesn't pass.
xmclaw backup verify pre-deploy --json | jq -e .ok >/dev/null \
  || { echo "backup corrupt, aborting"; exit 1; }
```

## 7. Doctor integration

`xmclaw doctor` includes a `BackupsCheck` (Epic #10 × Epic #20)
that's always `ok=True` but surfaces:

- No backups found yet → advisory pointing at `xmclaw backup create`.
- Newest backup < 30 days old → silent `N backup(s), newest 'X' Nd old`.
- Newest backup ≥ 30 days old → same summary + advisory to refresh.

`doctor --json` returns the age in the check's `detail` field so a
monitoring system can alert without `grep`ping human text.

## 8. What's **not** in Phase 1

Deferred items tracked on Epic #20 checklist:

- **Auto-daily scheduling** — needs the scheduler from Epic #4 wired to a
  cron-style trigger. You can get the same effect today via a system
  cron/launchd/Task Scheduler entry that runs `xmclaw backup create` +
  `xmclaw backup prune --keep 7 --yes`.
- **Zero-downtime reload** — `restore` will eventually coordinate with
  a `daemon/reloader.py` that drains the AgentLoop, swaps workspaces,
  and resumes without dropping live connections. Phase 1 needs the
  manual `stop` / `start` dance.
- **Remote / encrypted storage** — Phase 1 writes plain tar.gz onto
  the local filesystem. Push to S3 / restic / duplicity via your own
  cron wrapper; Epic #16 Phase 2 will encrypt the sensitive bits
  before they hit the archive.

## 9. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `create` hits `BackupError: name already exists` | Reusing a name | `--overwrite` or pick a new name |
| `restore` → `archive sha256 drift` | Storage bit-rot or interrupted write | Restore from an older backup; re-`verify` the survivor |
| `restore` → `manifest schema vN newer than supported` | Backup made by a newer `xmclaw` | `pip install -U xmclaw` on this machine, retry |
| `delete` → `invalid backup name` | `name` contained `/` or `..` | Use the short name from `list`, not the full path |
| `doctor` reports "backups folder not writable" | `~/.xmclaw/backups/` owned by another user | `chown -R $USER ~/.xmclaw/backups` |

## 10. Pointers

- Module code: [xmclaw/backup/](../xmclaw/backup/) — see its
  [AGENTS.md](../xmclaw/backup/AGENTS.md) for the import-direction
  contract.
- CLI: `xmclaw/cli/main.py`, search for `@backup_app.command`.
- Tests: `tests/unit/test_v2_backup.py` (60+ cases covering every
  subcommand, `--json` shape locks, tar-slip defence, bit-rot detection).
- Doctor check: `BackupsCheck` in `xmclaw/cli/doctor.py`.
- Roadmap: [DEV_ROADMAP.md §Epic #20](DEV_ROADMAP.md).
