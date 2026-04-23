# AGENTS.md — `xmclaw/providers/llm/`

## 1. 职责

Adapters between the `core.ir.toolcall` IR + `Message` types and
vendor LLM APIs. `base.py` holds `LLMProvider` ABC; `anthropic.py`
and `openai.py` are the two shipped providers; `translators/` holds
one-way IR ↔ vendor-format converters (tool-call shape, system
prompts, message roles).

## 2. 依赖规则

- ✅ MAY import: `xmclaw.core.ir.*`, `xmclaw.utils.*`,
  `xmclaw.security.*`, `anthropic`, `openai`, `httpx`, stdlib.
- ❌ MUST NOT import: any sibling `providers/*` package,
  `xmclaw.daemon.*`, `xmclaw.cli.*`.

## 3. 测试入口

- Unit: `tests/unit/test_v2_anthropic_provider.py`,
  `test_v2_anthropic_translator.py`, `test_v2_openai_provider.py`,
  `test_v2_openai_translator.py`.
- Smart-gate lane: `llm`.
- Translators have a bidirectional fuzz test (CI-4) — if you add
  a new translator pair, mirror the fuzz pattern.

## 4. 禁止事项

- ❌ Don't make HTTP calls in `__init__`. Construct the SDK client
  lazily; first request is where the token gets validated.
- ❌ Don't hard-code model names in translators. The provider
  config carries `default_model`; translators should be
  model-agnostic.
- ❌ Don't log full message content at INFO. Prompts contain user
  data; keep them DEBUG-only and redact `api_key`.
- ❌ Don't swallow `RateLimitError` / `APIStatusError`. Let the
  AgentLoop's retry + budget gate see the exception — that's where
  the policy lives.

## 5. 关键文件

- `base.py` — `LLMProvider` ABC; every provider implements
  `complete()` (sync) or `stream()` (async generator).
- `anthropic.py` / `openai.py` — concrete providers.
- `translators/` — one module per vendor: IR → vendor format.
