# AGENTS.md — `xmclaw/security/`

## 1. 职责

Defense against *content-level* attacks on the LLM context:
prompt-injection scanning + redaction. `prompt_scanner.py` exposes
`scan_text(text)` (pure function returning `ScanResult`), `redact()`,
and the `PolicyMode` enum the AgentLoop uses to decide detect /
redact / block.

Pure library — no I/O, no bus, no daemon state. That's what makes it
trivial to unit-test and cheap to call on every tool result.

## 2. 依赖规则

- ✅ MAY import: Python stdlib (`re`, `dataclasses`, `enum`).
- ❌ MUST NOT import: any `xmclaw.*` package. The scanner is a leaf;
  consumers call in, never the other way.

## 3. 测试入口

- Unit: `tests/unit/test_v2_prompt_scanner.py` (26 tests: pattern
  catalogue, unicode invisibles, severity threshold, redact
  idempotence, 100KB perf smoke).
- Integration: `tests/integration/test_v2_prompt_injection.py`
  (7 tests: detect_only / redact / block flow through AgentLoop,
  factory config parse).
- Smart-gate lane: `security`.

## 4. 禁止事项

- ❌ Don't rename a `pattern_id`. These appear in event payloads
  + redaction placeholders (`[redacted:<pattern_id>]`); dashboards
  key on them. Tune the regex if needed, keep the id stable.
- ❌ Don't add patterns without severity + category. Three
  severities, three categories (`instruction_override`,
  `role_forgery`, `exfiltration`); introduce a new category only
  when it's genuinely orthogonal, not just another flavour of an
  existing one.
- ❌ Don't regress the "scan benign text is ~1ms for 100KB"
  perf smoke. The scanner runs on every tool result — growth here
  is an availability tax.
- ❌ Don't add BOM (U+FEFF) to the invisible-char regex. Windows
  text files leak BOMs into tool output; flagging them would drown
  the signal. See the comment in `_INVISIBLE_CHARS`.

## 5. 关键文件

- `prompt_scanner.py` — the single module. Everything else is
  re-export from `__init__.py`.
  - `PolicyMode` (detect_only / redact / block) — config contract.
  - `scan_text()` — pure function; returns `ScanResult`.
  - `redact()` — right-to-left splice, idempotent.
  - `_ALL_PATTERNS` — the regex catalogue (instruction_override,
    role_forgery, exfiltration).
