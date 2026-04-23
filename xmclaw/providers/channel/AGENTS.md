# AGENTS.md — `xmclaw/providers/channel/`

## 1. 职责

Transport channels that stream events out to clients. `base.py`
defines `Channel` ABC; `ws.py` is the WebSocket channel used by the
web UI + CLI. Future channels (Slack bot, Discord, Telegram) plug in
at this layer without touching `core/` or `daemon/`.

## 2. 依赖规则

- ✅ MAY import: `xmclaw.core.bus.*` (event types + payloads),
  `xmclaw.utils.*`, stdlib, `websockets` / `fastapi` WebSocket types.
- ❌ MUST NOT import: sibling `providers/*` packages,
  `xmclaw.daemon.*` (the daemon wires channels INTO the app; it
  doesn't reach down).

## 3. 测试入口

- Integration: `tests/integration/test_v2_daemon_replay.py`,
  `test_v2_events_api.py`.
- Smart-gate lane: currently rolled into `bus` / `daemon` lanes;
  add a dedicated `channel` lane if the count grows.

## 4. 禁止事项

- ❌ Don't assume the client stays connected. Every send must
  handle `ConnectionClosed`; buffering is the bus's job, not the
  channel's.
- ❌ Don't serialize events ad-hoc. Use the payload shape from
  `core/bus/events.py` + `json.dumps` with `default=str`; clients
  key on the schema.
- ❌ Don't add auth logic inside the channel. Pairing-token
  validation happens in the daemon's middleware BEFORE the
  WebSocket upgrade.

## 5. 关键文件

- `base.py` — `Channel` ABC.
- `ws.py` — WebSocket channel: event stream + backpressure.
