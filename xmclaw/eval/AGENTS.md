# AGENTS.md — `xmclaw/eval/`

## 1. 职责

A/B benchmark harness（Sprint 4 起）。把"让一个 XMclaw agent 跑某个
benchmark 套件并产出可比对的 pass_rate / mean_score / cost / latency"
抽象成 3 步：(1) 按 id 选 `BenchmarkSuite`，(2) 给 `Runner` 一个
`agent_factory()`（每个 task 造一个全新 agent），(3) 读 `SuiteResult`。

eval 是 **batch one-shot 工具**，不是常驻运行时——它自建一个用完即弃的
agent context，由 CLI（`xmclaw eval`，见 `xmclaw/cli/eval.py`）驱动。

## 2. 依赖规则

- ✅ MAY import: `xmclaw.core.*`、`xmclaw.providers.*`、
  `xmclaw.utils.*`、stdlib（`asyncio` 等）。
- ❌ MUST NOT import: `xmclaw.daemon.*`。daemon 是长驻运行时；eval 只接
  受一个返回"带 `arun`/`run_turn` 的对象"的 callable。真实 CLI 用法由
  **调用方**（`xmclaw/cli/eval.py`）去 `daemon.factory.
  build_agent_from_config`，本包不碰那个 import。

## 3. 测试入口

- Unit: `tests/unit/test_v2_eval_harness.py`、
  `test_v2_longmemeval_full.py`、`test_v2_longmemeval_hard.py`、
  `test_v2_evolution_evaluation_trigger.py`。
- Manual smoke: `xmclaw eval --list`（列出已注册套件）、
  `xmclaw eval <suite_id>`。

## 4. 关键不变量 / 禁止事项

- **Per-task isolation 是 load-bearing 不变量**：`Runner` 对每个 task
  调 `agent_factory()` 造全新 agent，确保上个 task 的 memory / hop 状态
  不串味；单个 task 抛异常会被 catch 成
  `TaskResult(passed=False, error=...)`，不毒化整个套件。改 Runner 时不
  能破坏这两条。
- **加新套件 = 追加一条 registry，不动 CLI**：subclass
  `BenchmarkSuite`（自带 task 列表 + `grade()`），然后在 `__init__.py`
  的 `SUITE_REGISTRY` 里登记 `Suite.SUITE_ID → Suite`。⚠ 历史教训：
  套件写了却忘记登记 registry（见 commit 83e66d5 → 2015b4b 的 re-apply）
  ——加套件**必须**同时改 `SUITE_REGISTRY` 并补一条 import + `__all__`。
- ❌ 别在套件里假设 `expected_signals` 的 schema 跨套件统一——它是
  suite-specific（LongMemEval 用 `{"answer": ...}`，SWE-bench 风格用
  `{"unified_diff": ...}`）。`Runner` 不解释这个字段，只有套件的
  `grade()` 解释。

## 5. 关键文件

- `harness.py` — `TaskCase` / `TaskResult` / `SuiteResult` dataclass、
  `BenchmarkSuite` ABC、`Runner`（编排 + per-task 隔离）。
- `__init__.py` — `SUITE_REGISTRY`（CLI 能 list/run 的全部套件）。
- 套件实现: `longmemeval.py`（mini, 7 条手写多轮对话）、
  `longmemeval_full.py`、`longmemeval_hard.py`、`local_coding.py`、
  `swe_bench_verified.py`、`swe_bench_sandbox.py`、`terminal_bench.py`。
- ⚠ 根级 `ops.py` 与本包内 `fact.py` 是 `LocalCodingSuite` 之类"修这段
  代码"基准的样本目标文件 / 临时产物（untracked）——不是包逻辑，别误删
  也别 import。
