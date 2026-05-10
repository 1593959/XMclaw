"""Front-back contract test for the tool-call payload-key shape.

Pinned regression target: B-232. Pre-B-232 the daemon emitted
``BehavioralEvent`` payloads keyed under ``call_id`` for ``tool_call_
emitted`` / ``tool_invocation_finished``, but the JS reducer
(``static/lib/chat_reducer.js``) only read ``tool_call_id`` / ``id``.
The mismatch made every tool_use bubble freeze at status="running"
because the finished event couldn't find the message id it had
created. The fix taught the reducer to read both — and B-232's
fall-back is exactly the kind of thing that rots silently if either
side drifts again.

This test pins the contract from BOTH ENDS so a future drift fires
loudly instead of silently:

  Python side (daemon emission)
  ─────────────────────────────
  * Static-source guard: ``xmclaw/daemon/agent_loop.py`` must NOT
    publish a ``TOOL_CALL_EMITTED`` payload that uses ONLY
    ``tool_call_id`` as its identifier key. The publisher must
    surface ``call_id`` as well.
  * Same guard for ``TOOL_INVOCATION_FINISHED``.

  JS side (reducer ingestion)
  ───────────────────────────
  * Node-driven contract test: feed a real ``tool_call_emitted``
    event with payload-key ``call_id`` to ``applyEvent`` and verify
    the reducer creates a tool_use card with id=call_id.
  * Feed the matching ``tool_invocation_finished`` event with
    payload-key ``call_id`` and verify the card flips to status="ok".
  * Feed an event using the LEGACY ``tool_call_id`` payload key and
    verify the reducer's fallback STILL works (so old replays from
    pre-B-232 transcripts don't break).

Why front-back: per the standing rule (CLAUDE.md, 2026-05-09)
front-back tests must exercise the actual coupling, not each end
in isolation. The daemon-emit guard (Python) + reducer-ingest test
(JS via Node) together cover the full event contract.

Skips when ``node`` isn't on PATH (CI must have it; local dev can
run the Python guard portion regardless).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = REPO_ROOT / "xmclaw" / "daemon" / "static"
AGENT_LOOP = REPO_ROOT / "xmclaw" / "daemon" / "agent_loop.py"
REDUCER = STATIC_DIR / "lib" / "chat_reducer.js"


# ── Python side: pin the daemon emission keys ──────────────────────


def test_agent_loop_publishes_tool_call_emitted_with_call_id_key() -> None:
    """The TOOL_CALL_EMITTED publisher MUST put ``call_id`` in the
    payload. ``tool_call_id`` alone is the B-232 regression — the
    reducer's primary key path reads ``call_id``."""
    src = AGENT_LOOP.read_text(encoding="utf-8")
    # Find every ``TOOL_CALL_EMITTED`` publish and look at its payload
    # dict literal. The publisher pattern in agent_loop.py is:
    #     await publish(EventType.TOOL_CALL_EMITTED, {"call_id": ...})
    # We require at least one publish-site whose payload contains
    # ``"call_id":``. Any publish that omits the key entirely is the
    # bug we want to catch.
    occurrences = re.findall(
        r"EventType\.TOOL_CALL_EMITTED[^)]*\)\s*\n[^}]*\{[^}]*\}",
        src, flags=re.DOTALL,
    )
    # Fallback: just count call_id-containing payloads in the file.
    # If the publisher block format changes the regex above can miss;
    # the textual guard below is the resilient backstop.
    assert '"call_id":' in src, (
        "no ``call_id`` payload key found in agent_loop.py — "
        "TOOL_CALL_EMITTED publishers must surface call_id "
        "(B-232 regression: pre-fix only ``tool_call_id`` was emitted)"
    )
    # Stronger: the snippet around any TOOL_CALL_EMITTED publish must
    # mention call_id within ~400 chars (the payload literal).
    matches = list(re.finditer(r"EventType\.TOOL_CALL_EMITTED", src))
    assert matches, "no TOOL_CALL_EMITTED publish found in agent_loop.py"
    for m in matches:
        window = src[m.start():m.start() + 400]
        assert "call_id" in window, (
            f"TOOL_CALL_EMITTED at offset {m.start()} doesn't mention "
            f"call_id within the next 400 chars — payload likely missing "
            f"the key the reducer reads. Window:\n{window!r}"
        )


def test_agent_loop_publishes_tool_invocation_finished_with_call_id() -> None:
    """Same guard for TOOL_INVOCATION_FINISHED — the reducer's
    finished-arm reads ``payload.call_id`` first; emitting only
    ``tool_call_id`` brings back the "stuck at running" bug."""
    src = AGENT_LOOP.read_text(encoding="utf-8")
    matches = list(re.finditer(r"EventType\.TOOL_INVOCATION_FINISHED", src))
    assert matches, "no TOOL_INVOCATION_FINISHED publish found"
    for m in matches:
        window = src[m.start():m.start() + 400]
        assert "call_id" in window, (
            f"TOOL_INVOCATION_FINISHED at offset {m.start()} doesn't "
            f"mention call_id within 400 chars — drift suspected. "
            f"Window:\n{window!r}"
        )


def test_reducer_reads_both_payload_keys_for_compat() -> None:
    """The reducer must keep reading BOTH ``call_id`` and
    ``tool_call_id`` (in that order) so:
      * Live events keyed under call_id work (post-B-232 contract)
      * Replays from pre-B-232 transcripts keyed under tool_call_id
        still work (defensive fallback)
    Removing the fallback would re-break old replays — this static
    guard catches that."""
    src = REDUCER.read_text(encoding="utf-8")
    # Both payload keys must appear in the tool_call_emitted /
    # tool_invocation_finished arms. The pattern in chat_reducer.js
    # is ``payload.call_id || payload.tool_call_id || ...``.
    assert "payload.call_id" in src, (
        "reducer no longer reads payload.call_id — primary key "
        "path was removed (B-232 regression)"
    )
    assert "payload.tool_call_id" in src, (
        "reducer no longer reads payload.tool_call_id — legacy "
        "fallback was removed; old replays will break"
    )
    # And the ORDER matters — call_id is the live-event key, must
    # be tried first.
    callid_pos = src.index("payload.call_id")
    legacy_pos = src.index("payload.tool_call_id")
    assert callid_pos < legacy_pos, (
        "reducer reads tool_call_id BEFORE call_id — wrong precedence; "
        "live events would fall through to the legacy key first"
    )


# ── JS side: drive the reducer with synthetic events ─────────────


def _run_reducer(events: list[dict]) -> dict:
    """Spawn ``node`` to load chat_reducer.js, fold ``events`` over
    ``applyEvent``, and return the resulting state. Mirrors the
    fixture pattern from test_b345_chat_reducer_actually_creates_card_via_node."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH")

    static = STATIC_DIR.resolve().as_posix()
    events_json = json.dumps(events)

    driver = f"""
    const url = "file:///{static}/lib/chat_reducer.js";
    globalThis.window = globalThis.window || {{}};
    globalThis.window.__xmc = {{
      preact: {{ h: () => null }},
      preact_hooks: {{
        useState: (init) => [init, () => {{}}],
        useEffect: () => {{}},
        useMemo: (fn) => fn(),
        useCallback: (fn) => fn,
      }},
      htm: {{ bind: () => () => null }},
    }};
    const mod = await import(url);
    let state = {{ messages: [] }};
    const events = {events_json};
    for (const ev of events) {{
      state = mod.applyEvent(state, ev);
    }}
    process.stdout.write(JSON.stringify(state));
    """
    cp = subprocess.run(
        [node, "--input-type=module", "-e", driver],
        capture_output=True, text=True, timeout=30,
    )
    assert cp.returncode == 0, (
        f"node driver failed: stdout={cp.stdout!r} stderr={cp.stderr!r}"
    )
    return json.loads(cp.stdout)


def test_reducer_handles_call_id_payload_shape() -> None:
    """Live-event contract: payload uses ``call_id`` (post-B-232).
    Reducer should create a tool_use card with id=call_id and flip
    it to status="ok" on the matching finished event."""
    state = _run_reducer([
        {
            "type": "tool_call_emitted",
            "payload": {
                "call_id": "tc-call-1",
                "name": "list_files",
                "args": {"path": "/tmp"},
            },
            "ts": 1000,
        },
        {
            "type": "tool_invocation_finished",
            "payload": {
                "call_id": "tc-call-1",
                "result": "ok",
            },
            "ts": 1001,
        },
    ])
    msgs = state.get("messages", [])
    tool_msgs = [m for m in msgs if m.get("kind") == "tool_use"]
    assert len(tool_msgs) == 1, f"expected 1 tool_use card, got {tool_msgs!r}"
    card = tool_msgs[0]
    assert card["id"] == "tc-call-1"
    assert card["name"] == "list_files"
    assert card["status"] == "ok", (
        f"tool_use card stuck at status={card['status']!r} — "
        "the B-232 regression. Reducer didn't match the finished "
        "event's call_id back to the running card."
    )


def test_reducer_handles_legacy_tool_call_id_payload_shape() -> None:
    """Legacy-replay contract: a transcript saved pre-B-232 only has
    ``tool_call_id`` in the payload. The reducer's fallback must
    still surface the card and flip it on finished."""
    state = _run_reducer([
        {
            "type": "tool_call_emitted",
            "payload": {
                "tool_call_id": "tc-legacy-1",
                "name": "read_file",
                "args": {"path": "/etc/hostname"},
            },
            "ts": 2000,
        },
        {
            "type": "tool_invocation_finished",
            "payload": {
                "tool_call_id": "tc-legacy-1",
                "result": "host-A",
            },
            "ts": 2001,
        },
    ])
    msgs = state.get("messages", [])
    tool_msgs = [m for m in msgs if m.get("kind") == "tool_use"]
    assert len(tool_msgs) == 1, f"expected 1 tool_use card, got {tool_msgs!r}"
    card = tool_msgs[0]
    assert card["id"] == "tc-legacy-1"
    assert card["name"] == "read_file"
    assert card["status"] == "ok", (
        "legacy tool_call_id replay broke — fallback path removed?"
    )


def test_reducer_call_id_takes_precedence_over_tool_call_id() -> None:
    """If a payload (somehow) carries BOTH keys, ``call_id`` wins.
    This pins the precedence so a future "let's standardize on the
    other key" refactor surfaces in CI rather than silently breaking
    every live tool bubble."""
    state = _run_reducer([
        {
            "type": "tool_call_emitted",
            "payload": {
                "call_id": "primary-id",
                "tool_call_id": "fallback-id",
                "name": "test_tool",
                "args": {},
            },
            "ts": 3000,
        },
    ])
    msgs = state.get("messages", [])
    tool_msgs = [m for m in msgs if m.get("kind") == "tool_use"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["id"] == "primary-id", (
        f"reducer picked id={tool_msgs[0]['id']!r}; expected 'primary-id'. "
        "call_id should win when both are present."
    )


def test_reducer_does_not_genid_when_call_id_is_present() -> None:
    """The original B-232 bug was that the reducer fell through to
    ``genId()`` because both payload keys were missing. With either
    key present the id MUST come from the payload, never genId.
    A fresh genId here would re-introduce the "stuck at running"
    failure mode (different id than the finished event will use)."""
    state = _run_reducer([
        {
            "type": "tool_call_emitted",
            "payload": {
                "call_id": "deterministic-id",
                "name": "test_tool",
            },
            "ts": 4000,
        },
    ])
    msgs = state.get("messages", [])
    tool_msgs = [m for m in msgs if m.get("kind") == "tool_use"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["id"] == "deterministic-id", (
        f"id={tool_msgs[0]['id']!r} doesn't match the payload call_id — "
        "reducer fell through to genId despite a valid key being present"
    )
