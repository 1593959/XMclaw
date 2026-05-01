"""CLI commands for evolution visibility (Epic #4 Phase A) +
review / approve / reject (Epic #24 Phase 1).

Reads:
  * ``~/.xmclaw/skills/*.jsonl`` — promote / rollback history (Epic #4)
  * ``~/.xmclaw/v2/evolution/<agent_id>/decisions.jsonl`` — observer
    audit log written by EvolutionAgent (Epic #17 Phase 7)
  * ``~/.xmclaw/v2/events.db`` — SqliteEventBus, queried for
    SKILL_CANDIDATE_PROPOSED entries that have not yet been resolved
    by a matching SKILL_PROMOTED / SKILL_ROLLED_BACK (Epic #24)

Approve / reject hit the daemon over HTTP using the pairing token from
``~/.xmclaw/v2/pairing_token.txt``. Daemon must be running (``xmclaw
start``); we surface a clear error otherwise.
"""
from __future__ import annotations

import json
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

import typer

from xmclaw.utils.i18n import _
from xmclaw.utils.paths import data_dir, evolution_dir, skills_dir


def _parse_since(since: str | None) -> float | None:
    """Convert ``--since`` string like ``24h`` or ``7d`` to a unix timestamp."""
    if since is None:
        return None
    since = since.strip().lower()
    if since.endswith("h"):
        hours = int(since[:-1])
        return time.time() - hours * 3600
    if since.endswith("d"):
        days = int(since[:-1])
        return time.time() - days * 86400
    # Fallback: treat as integer hours
    try:
        return time.time() - int(since) * 3600
    except ValueError:
        return None


def _load_history_records(
    since_ts: float | None = None,
) -> list[dict[str, Any]]:
    """Load all promotion/rollback records from ``~/.xmclaw/skills/*.jsonl``."""
    records: list[dict[str, Any]] = []
    base = skills_dir()
    if not base.exists():
        return records
    for path in base.glob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = rec.get("ts")
            if since_ts is not None and ts is not None and ts < since_ts:
                continue
            records.append(rec)
    # Sort chronologically ascending
    records.sort(key=lambda r: r.get("ts", 0))
    return records


def _fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _fmt_record(rec: dict[str, Any]) -> str:
    """Format a single history record into a one-line summary."""
    kind = rec.get("kind", "?")
    skill_id = rec.get("skill_id", "?")
    from_v = rec.get("from_version", 0)
    to_v = rec.get("to_version", 0)
    ts = rec.get("ts", 0)
    evidence = rec.get("evidence", [])

    arrow = f"v{from_v} → v{to_v}" if from_v != to_v else f"v{to_v}"
    time_str = _fmt_ts(ts) if ts else "?"

    # Try to extract a grader mean score from evidence strings like "mean=0.723"
    score_str = ""
    for ev in evidence:
        if isinstance(ev, str) and "mean=" in ev:
            try:
                score = float(ev.split("mean=", 1)[1])
                score_str = _("evolution.score_label", score=score)
                break
            except ValueError:
                pass

    if kind == "promote":
        return f"  [+] {time_str}  {skill_id:<24} {arrow}{score_str}"
    if kind == "rollback":
        reason = rec.get("reason", "")
        reason_str = _("evolution.reason_label", reason=reason) if reason else ""
        return f"  [-] {time_str}  {skill_id:<24} {arrow}{reason_str}"
    return f"  [?] {time_str}  {skill_id:<24} {arrow}"


def run_evolution_show(since: str | None = None) -> int:
    """Print evolution history as a formatted table."""
    since_ts = _parse_since(since)
    records = _load_history_records(since_ts)

    if not records:
        typer.echo(_("evolution.no_events"))
        if since:
            typer.echo(_("evolution.filtered_since", since=since))
        return 0

    header = f"{_('evolution.header_time'):<18} {_('evolution.header_skill'):<24} {_('evolution.header_change')}"
    typer.echo(header)
    typer.echo("-" * len(header))
    for rec in records:
        typer.echo(_fmt_record(rec))
    return 0


# ── Epic #24 Phase 1: review / approve / reject ─────────────────────


def _events_db_path() -> Path:
    return data_dir() / "v2" / "events.db"


def _pairing_token() -> str | None:
    """Read the daemon's pairing token. Returns None if missing."""
    p = data_dir() / "v2" / "pairing_token.txt"
    try:
        return p.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _query_pending_candidates() -> list[dict[str, Any]]:
    """Find SKILL_CANDIDATE_PROPOSED events without a matching
    SKILL_PROMOTED / SKILL_ROLLED_BACK on the same winner_candidate_id
    after they were proposed.

    Returns list of dicts with: ts, candidate_id, winner_version,
    decision (promote|rollback), evidence, reason, event_id.
    """
    db = _events_db_path()
    if not db.is_file():
        return []
    proposed: list[dict[str, Any]] = []
    resolved: dict[tuple[str, int], float] = {}

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
    try:
        cur = con.execute(
            "SELECT id, ts, type, payload FROM events "
            "WHERE type IN ("
            "'skill_candidate_proposed','skill_promoted','skill_rolled_back'"
            ") ORDER BY ts ASC"
        )
        for ev_id, ts, etype, payload_raw in cur:
            try:
                p = json.loads(payload_raw or "{}")
            except json.JSONDecodeError:
                continue
            if etype == "skill_candidate_proposed":
                cid = p.get("winner_candidate_id")
                ver = p.get("winner_version")
                if cid is None or ver is None:
                    continue
                proposed.append({
                    "ts": ts,
                    "event_id": ev_id,
                    "candidate_id": str(cid),
                    "winner_version": int(ver),
                    "decision": p.get("decision", "promote"),
                    "evidence": list(p.get("evidence", []) or []),
                    "reason": p.get("reason", ""),
                })
            elif etype in ("skill_promoted", "skill_rolled_back"):
                cid = p.get("skill_id") or p.get("winner_candidate_id")
                ver = p.get("to_version") or p.get("winner_version")
                if cid is not None and ver is not None:
                    key = (str(cid), int(ver))
                    # Latest resolution wins (we re-iterate later anyway).
                    resolved[key] = ts
    finally:
        con.close()

    pending: list[dict[str, Any]] = []
    for rec in proposed:
        key = (rec["candidate_id"], rec["winner_version"])
        # A proposal is pending when no resolution event with same
        # (skill_id, to_version) was emitted AFTER the proposal.
        resolved_ts = resolved.get(key)
        if resolved_ts is None or resolved_ts < rec["ts"]:
            pending.append(rec)
    return pending


def run_evolve_review(as_json: bool = False) -> int:
    """List pending SKILL_CANDIDATE_PROPOSED entries (no auto_apply gate)."""
    pending = _query_pending_candidates()
    if as_json:
        typer.echo(json.dumps(pending, ensure_ascii=False, indent=2))
        return 0
    if not pending:
        typer.echo("没有待审进化候选 (no pending evolution candidates).")
        typer.echo(
            "Tip: 候选由 daemon 内的 EvolutionAgent observer 在 grader "
            "verdict 累积到阈值后自动产出。"
        )
        return 0
    typer.echo(
        f"{'TS':<18} {'CANDIDATE':<28} {'V':<4} {'DECISION':<10} EVIDENCE"
    )
    typer.echo("-" * 88)
    for rec in pending:
        ts_str = datetime.fromtimestamp(rec["ts"]).strftime("%Y-%m-%d %H:%M")
        ev_str = ", ".join(rec["evidence"])[:60]
        typer.echo(
            f"{ts_str:<18} {rec['candidate_id']:<28} "
            f"v{rec['winner_version']:<3} {rec['decision']:<10} {ev_str}"
        )
    typer.echo("")
    typer.echo(
        f"{len(pending)} 个待审。下一步：xmclaw evolve approve <candidate_id> "
        "或 xmclaw evolve reject <candidate_id> --reason '...'"
    )
    return 0


def _http_call(
    method: str, path: str, *, body: dict[str, Any] | None = None,
    daemon_url: str = "http://127.0.0.1:8765",
) -> tuple[int, dict[str, Any]]:
    """Call daemon HTTP with pairing-token auth. Returns (status, body)."""
    token = _pairing_token()
    if token is None:
        return 0, {
            "error": (
                "no pairing token at ~/.xmclaw/v2/pairing_token.txt — "
                "daemon must be running (xmclaw start)."
            ),
        }
    url = daemon_url + path
    data = json.dumps(body or {}).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(raw or "{}")
            except json.JSONDecodeError:
                return resp.status, {"raw": raw}
    except urllib.error.HTTPError as e:
        try:
            body_str = e.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            body_str = ""
        try:
            body_obj = json.loads(body_str or "{}")
        except json.JSONDecodeError:
            body_obj = {"raw": body_str}
        return e.code, body_obj
    except urllib.error.URLError as e:
        return 0, {"error": f"daemon unreachable: {e.reason}"}


def run_evolve_approve(candidate_id: str) -> int:
    """Approve a pending candidate by routing through evidence-gated promote.

    Looks up the most recent SKILL_CANDIDATE_PROPOSED for ``candidate_id``,
    forwards its evidence list to ``POST /api/v2/skills/<id>/promote``.
    Anti-req #12 still enforced server-side at the registry door.
    """
    pending = _query_pending_candidates()
    matches = [p for p in pending if p["candidate_id"] == candidate_id]
    if not matches:
        typer.echo(
            f"找不到待审候选 candidate_id={candidate_id}。"
            "运行 'xmclaw evolve review' 看可审列表。"
        )
        return 1
    # Pick the most recent (largest ts).
    rec = max(matches, key=lambda r: r["ts"])
    if rec["decision"] == "rollback":
        path = f"/api/v2/skills/{candidate_id}/rollback"
        body = {
            "to_version": rec["winner_version"],
            "reason": rec.get("reason") or "approved via xmclaw evolve approve",
        }
    else:
        path = f"/api/v2/skills/{candidate_id}/promote"
        body = {
            "to_version": rec["winner_version"],
            "evidence": rec["evidence"] or [
                f"approved-via-cli ts={rec['ts']:.0f} reason={rec.get('reason','')[:60]}",
            ],
        }
    status, resp = _http_call("POST", path, body=body)
    if status == 0:
        typer.echo(f"[ERROR] {resp.get('error', 'unknown')}")
        return 1
    if status >= 400:
        typer.echo(f"[FAIL] HTTP {status}: {resp}")
        return 1
    typer.echo(
        f"[OK] {rec['decision']} {candidate_id} → v{rec['winner_version']}: {resp}"
    )
    return 0


def run_evolve_reject(candidate_id: str, reason: str) -> int:
    """Record a rejection in the local audit log.

    Does NOT mutate SkillRegistry — a rejection is "leave HEAD alone".
    Writes to ``~/.xmclaw/v2/evolution/<agent_id>/rejections.jsonl`` so
    the audit chain shows BOTH directions of decisions.
    """
    if not reason or not reason.strip():
        typer.echo("[ERROR] --reason 必填，描述拒绝原因。")
        return 2
    audit_dir = evolution_dir() / "evo-main"
    audit_dir.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": time.time(),
        "kind": "rejection",
        "candidate_id": candidate_id,
        "reason": reason.strip(),
    }
    path = audit_dir / "rejections.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    typer.echo(f"[OK] rejection logged → {path}")
    return 0
