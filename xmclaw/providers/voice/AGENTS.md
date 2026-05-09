# AGENTS.md — `xmclaw/providers/voice/`

## 1. 职责

Voice providers — STT (speech → text) + TTS (text → speech).
`base.py` defines `STTProvider` + `TTSProvider` ABCs; concrete
implementations:

* `whisper.py` — local STT via `faster-whisper` (CPU-friendly, no
  API key, multilingual).
* `edge_tts.py` — TTS via Microsoft Edge's free voice service
  (no API key, hundreds of multilingual voices).

The tool layer (`providers/tool/builtin.py`) wires `voice_transcribe`
and `voice_synthesize` to these providers when the daemon factory
constructs them from the `config.voice` block.

## 2. 依赖规则

- ✅ MAY import: `xmclaw.utils.*`, stdlib, third-party voice SDKs
  (`faster-whisper`, `edge-tts`) — both lazy-imported inside the
  constructor / first-call to keep the package importable without
  the optional extra.
- ❌ MUST NOT import: sibling `providers/*` packages (use ABC
  contracts only), `xmclaw.daemon.*`, `xmclaw.cli.*`,
  `xmclaw.skills.*`.

## 3. 测试入口

- Unit: `tests/unit/test_v2_voice_providers.py`.
- Manual smoke: with the `[voice]` extra installed,
  ``python -c "import asyncio; from xmclaw.providers.voice import EdgeTTS; print(len(asyncio.run(EdgeTTS().synthesize('你好世界'))))"``
  should print a non-zero number of mp3 bytes.

## 4. 禁止事项

- ❌ Don't import the SDK at module scope. The whole point of the
  `[voice-stt]` / `[voice-tts]` extras is that a fresh `pip install
  xmclaw` doesn't pull faster-whisper / edge-tts. Import inside the
  constructor / first-use method, raise `ImportError` with an
  actionable `pip install ...` hint.
- ❌ Don't run the heavy work (model load, network call) on the
  event loop directly. faster-whisper is sync + CPU-bound — wrap in
  `asyncio.to_thread`. edge-tts is already async.
- ❌ Don't bake credentials into the provider — neither backend
  needs an API key today. If a future provider does, accept it via
  the constructor and never log it.

## 5. 关键文件

- `base.py` — `STTProvider.transcribe(audio_bytes) -> str`,
  `TTSProvider.synthesize(text, voice) -> bytes`.
- `whisper.py` — `WhisperSTT(model_name="tiny", device="cpu",
  compute_type="int8", language=None)`.
- `edge_tts.py` — `EdgeTTS(voice="zh-CN-XiaoxiaoNeural", rate="+0%",
  volume="+0%")`.
