# XMclaw v2 — 开发与 Plan（2026-04-21）

> **Status snapshot, 2026-04-21 end of day** (see §11 for the full scorecard):
>
> The v2 spine — streaming observer + honest grader + unified IR + online
> scheduler + versioned skill registry + autonomous evolution controller —
> is **live-validated on real LLM**. Session-level self-evolution lifts mean
> reward by **18%** on MiniMax end-to-end (Phase 3.5, commit `3f44d6d`).
>
> **293 unit/integration tests pass. Three live benches pass** (Phase 1.3,
> 2.6, 3.5). The 14 anti-requirements have 11 proven in code, 3 deferred.
> The v2-rewrite branch carries the full implementation; it has NOT been
> merged to main (see §12).

---

## 1. 为什么做 v2

三家 peer（OpenClaw 361k⭐、Hermes 106k⭐、QwenPaw 15k⭐）已经把"个人 AI 助手 runtime"这条赛道的 commodity 部分做到用户层几乎无差。XMclaw v1 与它们定位相同，但：

- 基础能力实现每一层都在独立造轮子 → 同样的底层 bug 我们自己重踩一遍（6 个 fix 就是证据）
- 差异化标签"自我进化"被 Hermes 以 DSPy+GEPA 抢跑且更扎实
- 演化引擎目前产出具有误导性的 auto-commit

**结论：** v1 继续打补丁是 dominated strategy。v2 做两件事——commodity 层直接照抄，差异化层把"系统级进化"做到独一份。

---

## 2. Positioning（一句话）

> **A self-evolving agent where evolution IS the runtime — streaming, continuous, OS-level — on an open, provider-agnostic framework that doesn't degrade the underlying model's intelligence.**

三条支柱：

1. **同模型用 XMclaw 不能比用别家差。** 用户实感：v1 上同一个 LLM 表现明显不如在 Hermes/OpenClaw/QwenPaw。这是一个直接的正交 bench 项——我们的编排/prompt/pipeline 不应该让模型变蠢。
2. **真的自进化，不是剧场式。** v1 的 GeneForge/SkillForge 产出过误导性 commit——这就是"自进化"不可证伪的症状。v2 第一天就要有可测量的进化指标（同任务第 1 次 vs 第 50 次的分数曲线），不达标就不叫进化。
3. **开放与兼容是底层不变量。** 一切 provider（LLM、memory、tool、channel、skill runtime）皆接口化；事件 schema 公开、版本化；支持 MCP / ACP / OpenAI-compat / Anthropic-compat；不绑单一厂商、不绑单一 OS。

### Commodity vs 差异化
| 层 | 策略 |
|---|---|
| Channels / IM adapters | **Copy Hermes** 的 adapter/protocol 分层 |
| Memory provider 接口 | **Copy Hermes** #3943 提议（pluggable） |
| Skill runtime 骨架 | **Copy Hermes** DSPy 内部布局 |
| 安全 guards | **Copy QwenPaw** 事后加固 |
| Onboarding UX + Canvas | **Copy OpenClaw** onboard CLI |
| Release engineering（channel 打包） | **反向学习三家**——他们都掉 channel，我们 CI 强制 parity |
| **Streaming evolution 运行时** | **原创** |
| **Honest self-eval ground truth** | **原创** |
| **Skills as OS-primitive processes** | **原创** |
| **Hot-reload 自修改 runtime** | **原创** |

---

## 3. 非目标（v2 不做）

- 重写 LLM 推理（用现成 provider，router 照旧）
- 造自己的 vector store（sqlite-vec 够用，或借 Hermes 的 memory interface）
- 造自己的 IM 协议（全部 adapter 到既有协议）
- 同时支持 N 种自定义 DSL（进化的"程序"= Python skill + prompt，不造新语言）

---

## 4. Anti-requirements（硬红线——v2 第一天就不能有）

来自 [project_peer_pain_points.md](../../../../.claude/projects/C--Users-15978-Desktop-XMclaw/memory/project_peer_pain_points.md) 10 条。每条对应一个不可妥协的 invariant：

### Peer 来的 10 条（外部锚点）
| # | Anti-req | Invariant |
|---|---|---|
| 1 | 不能信任"描述了 tool call 的文本" | Scheduler 层强制：claimed action == executed action，否则 reject |
| 2 | 不能是扁平/反应式/FTS5-only 记忆 | 语义检索 + 分层 + 不冻结在 system prompt |
| 3 | 不能 per-provider 翻译器易碎 | 统一内部 Tool-Call IR，每个 provider 有 native 测试矩阵 |
| 4 | 不能用 LLM 自评当真分数 | 真 ground-truth 检查：ran? returned? type match? side-effect observable? |
| 5 | 不能技能无回滚 | 技能=版本化制品，auto-gen 永不静默覆盖 hand-edit |
| 6 | 不能成本失控不可见 | 实时成本计数 + 硬预算熔断（v1 cost_tracker 扩展） |
| 7 | 不能 release 静默掉 channel | CI 矩阵测全 channel，否则 block release |
| 8 | 不能本地 WS 裸跑 | 配对时绑定设备密钥（防 ClawJacked）+ 技能资源上限 |
| 9 | 不能 session 泄状态 | Session = OS 一等公民，显式 lifecycle |
| 10 | 不能多后端同构性不足 | 声明 N 个 backend 就必须跑同一套 conformance test |

### 我们自己 v1 来的 4 条（内部血泪）
| # | Anti-req | Invariant |
|---|---|---|
| 11 | **同模型用 XMclaw 不得比用 peer 差** | 每个 release 跑同模型对照 bench（XMclaw vs Hermes vs OpenClaw vs 裸 provider）。平均分劣于裸 provider ≥5% 即 block release。编排层只能让模型变聪明，不能变蠢。 |
| 12 | 进化不能是剧场式 | 每条 auto-commit / skill-promote 必须带可验证分数提升证据（grader 跑分 + 前后对照 + 可 rollback）。不带证据的演化提交在 CI 被 reject。"我进化了" ≠ 进化。 |
| 13 | **底层必须开放 + 可扩展** | 所有 provider（LLM、memory、channel、tool、skill runtime、grader、scheduler）都是已定义接口；有二方 plugin SDK + 一方参考实现。任何核心路径都不能硬编码单一厂商。 |
| 14 | **底层必须兼容 + 跨平台** | 支持 MCP / ACP / OpenAI-compat / Anthropic-compat / OpenAI tool-call shape / Anthropic tool-call shape；Windows / macOS / Linux 三平台 conformance 测试；事件 schema 公开 + semver 管理。 |

---

## 5. 架构一览

```
                    ┌─────────────────────────────┐
                    │   Streaming Observer Bus    │◄─ 所有事件进这里
                    │  (event stream, 持久化)      │
                    └──────────┬──────────────────┘
                               │
              ┌────────────────┼─────────────────────┐
              ▼                ▼                     ▼
       ┌──────────┐      ┌──────────┐         ┌──────────┐
       │Evolution │      │  Honest  │         │Cross-sess│
       │Scheduler │      │ Grader   │         │ Memory   │
       │(continuous)│    │(ground   │         │(semantic │
       │          │      │ truth)   │         │ +hierar.)│
       └────┬─────┘      └────┬─────┘         └────┬─────┘
            │                 │                     │
            └─────────┬───────┴──────────┬──────────┘
                      ▼                  ▼
               ┌──────────────┐   ┌──────────────┐
               │  Skills as   │   │  Hot-Reload  │
               │ OS processes │   │   Runtime    │
               │(fork/exec/   │   │ (patch w/o   │
               │ kill, ver,   │   │  restart)    │
               │ rollback)    │   │              │
               └──────┬───────┘   └──────┬───────┘
                      │                  │
                      └────────┬─────────┘
                               ▼
                    ┌──────────────────┐
                    │ Commodity Layer  │◄─ Copy from peers
                    │ Channels/Memory/ │
                    │ Tools/LLM router │
                    └──────────────────┘
```

**关键约束：** Commodity 层向上只通过 Observer Bus 发事件；不能直接 bypass Scheduler/Grader 调 LLM。这条约束是差异化能落地的前提。

---

## 6. 阶段路线图

### Phase 0 — Salvage 审计 + 新骨架（1 周）

**目标：** 决定 v1 什么留、什么弃；把 v2 repo 结构搭起来。

- [ ] 逐模块审计 v1（见 §7 预评）
- [ ] 新建分支 `v2-rewrite`；monorepo 骨架：`core/`, `commodity/`, `evolution/`, `channels/`, `cli/`, `web/`
- [ ] 定义 Event schema（Observer Bus 事件类型）：`tool_call`, `tool_result`, `llm_request`, `llm_response`, `skill_exec`, `skill_promote`, `grader_verdict`, `cost_tick`, `session_event`
- [ ] 定义 Tool-Call IR（统一内部格式，per-provider translator）
- [ ] CI 骨架：pytest + conformance-test harness（Phase 2+ 要用）

**退出门：** 能跑 `xmclaw2 --help`，Event schema 冻结。

### Phase 1 — 差异化最小纵切（2 周，决定性阶段）

**目标：** 证明"streaming evolution + honest self-eval"在单个 CLI agent 上跑得通，并且真的把 agent 变得比上一轮好。

- [ ] Observer Bus：内存实现 + SQLite 持久化
- [ ] Honest Grader：ground-truth 检查器（初版只查 ran/returned/type，不查语义）
- [ ] Evolution Scheduler：每个 tool-call 都打分，得分进优化候选池
- [ ] Programmatic Optimization Loop：仿 DSPy 编译 prompt 作为可训练程序，但**在线增量**更新（区别于 Hermes 批处理）
- [ ] 一个 demo skill（比如"读文件并摘要"）从初始 prompt 出发，跑 50 turn 后能被优化器显著改进
- [ ] 端到端测试：同一任务在第 1 次和第 50 次表现，得分曲线上升

**退出门（=go/no-go）：** Demo skill 成绩曲线单调上升，且 grader 判分与人工判分一致率 ≥ 80%。  
若失败 → 差异化思路重估，可能回到"基于 Hermes 做中文 vertical"的 fallback。

### Phase 2 — Commodity 层（3 周）

Copy / adapt 自 peers，但每个都内置 anti-requirement guard。

- [ ] Channel adapters：WS、Slack、Discord、Telegram、飞书、企业微信、QQ（借 Hermes adapter 模板）
  - CI conformance：每个 channel 跑同一份消息收发/重连/丢帧测试
- [ ] Memory provider 接口（借 Hermes #3943 设计）：默认实现 = sqlite-vec + 分层（短期/工作/长期）
  - 不冻结在 system prompt
- [ ] Skill runtime 布局（借 Hermes）：但每个 skill 有 manifest.json（权限、资源上限）
- [ ] LLM router：provider-agnostic，Tool-Call IR 统一
- [ ] Cost tracker + 硬预算熔断（v1 已有，迁移扩展）

**退出门：** Channel 矩阵全绿，memory 语义检索 recall@10 > baseline，预算超限真熔断。

### Phase 3 — Skills as OS primitives（2 周）

- [ ] Skill = 可 fork/exec/kill 的进程
- [ ] 版本化 + rollback（`skill rollback <id>`，不走 git）
- [ ] Manifest-driven 权限沙箱（文件/网络/子进程 allow-list）
- [ ] Auto-gen 永不静默覆盖 hand-edit（promote 需要显式 signal）

**退出门：** 恶意 skill 跑满资源被熔断；rollback 可 <1s 还原。

### Phase 4 — Evolution-as-Scheduler（2 周）

把 Phase 1 的 loop 从"demo skill 上面"扩到"全局每个 LLM 调用/tool 选择"：

- [ ] Tool 选择本身是可优化决策
- [ ] Prompt 每个字段是可编译程序
- [ ] 跨 session 的梯度（昨天的经验改善今天的路由）

**退出门：** 全局 A/B：evolution 开启 vs 关闭，在 N 个真实任务上胜率显著。

### Phase 5 — Hot-reload self-modifying runtime（1 周）

- [ ] LLM 写出的 patch → grader 过 → 沙箱 dry-run → 热装载（无 daemon 重启）
- [ ] Patch 失败自动回滚

**退出门：** 一轮对话内 agent 修自己的 skill 并立即生效，不重启。

### Phase 6 — 生态与发布（2 周）

- [ ] Web UI（借 `hermes-webui` 布局灵感，但绑定 v2 event schema）
- [ ] Desktop tray（保留 v1 做法）
- [ ] `xmclaw onboard` CLI（借 OpenClaw 体验）
- [ ] 安装器 + PyPI 发布

**退出门：** 一条 curl 装、一条命令启、跑三家 peer 的对标 demo。

### 总时间预估

13 周单人全职。Phase 1 是最大单点风险——建议独立花 2–3 周把它跑通，再承诺后面阶段。

---

## 7. v1 Salvage map（更新：全面重构 = 激进弃）

用户直指："从头到尾就照着他们三家的去重构"。默认策略变成**弃**，除非该文件经过证伪检验、是纯数据/身份、或是无争议的 util。

### 保留（原样或近乎原样迁移）
| 文件/目录 | 原因 |
|---|---|
| `SOUL.md` / `PROFILE.md` / `AGENTS.md` | 身份/人设数据，v1/v2 都用 |
| `agents/*/PROFILE.md` + `SOUL.md` | 用户生成的 agent 档案 |
| `shared/memories/` + `shared/profiles/` 的用户数据 | 用户积累的记忆内容（数据迁移脚本另写） |
| `xmclaw/utils/log.py`、`xmclaw/utils/paths.py` | 纯 util，无业务耦合 |
| `daemon/config.example.json` 的 schema 形状 | 作为 v2 config schema 参考起点 |
| `xmclaw/llm/anthropic_client.py`、`openai_client.py` 的 **usage 捕获逻辑** | 刚 landing + 真机验证过，Phase 2 LLM adapter 里再用一次 |
| `xmclaw/core/cost_tracker.py` | Fix 6 验证过，扩展成硬熔断即可 |
| `scripts/build_exe_fast.py` + `xmclaw_setup.iss` | Windows 打包链条能用就不造 |

### 弃（v2 重写）
| 目录 | 理由 |
|---|---|
| `xmclaw/core/agent_loop.py` | 5-阶段 pipeline 与 evolution-as-runtime 正交，重写 |
| `xmclaw/core/task_classifier.py` | 固定分类不再是主轴，scheduler 决定路由 |
| `xmclaw/core/prompt_builder.py` | 组装逻辑改为可优化程序，硬编码 prompt 段落不要 |
| `xmclaw/core/reflection.py`（及其它 core） | 流程换了 |
| `xmclaw/evolution/*`（GeneForge / SkillForge / VFM / Validator / Scheduler） | 剧场式进化的源头，推倒 |
| `xmclaw/genes/*` | 基因"概念"保留，实现重做 |
| `xmclaw/memory/*` | 重建：分层 + 语义 + pluggable provider 接口（照抄 Hermes MemoryProvider 提议） |
| `xmclaw/tools/*` | 按 MCP + 内部 Tool-Call IR 重建 |
| `xmclaw/integrations/*` | 按 v2 channel adapter 协议全部重写，每个补 CI conformance |
| `xmclaw/gateway/*` | 用 v2 event schema 重写 |
| `xmclaw/daemon/server.py` | 仍是 FastAPI+WS，但重写：设备绑定认证、事件总线接口、无历史包袱 |
| `xmclaw/llm/router.py` | provider 路由重写，Tool-Call IR 作核心抽象 |
| `xmclaw/sandbox/*` | 升级为 manifest-driven process isolation，当前实现不够 |
| `xmclaw/desktop/*` | 和 daemon 解耦重写，托盘 + 浏览器启动器 |
| `xmclaw/cli/*` | 命令命名可借鉴，代码全部重写对接 v2 |
| `web/*` | **前端从零重构**。新 event schema、新交互 flow、新 UX。用户明确表态 UI 槽点满满 |
| `xmclaw/daemon/orchestrator.py` 及其他 daemon 内部 | 全部重写 |

### 参考抄录（从 peer 直接搬模式，不是搬代码）
| 功能 | 抄自 |
|---|---|
| Channel adapter 协议 | Hermes |
| MemoryProvider 接口 | Hermes #3943 |
| Skill manifest.json | OpenClaw #28298 / #28360 |
| `onboard` CLI 流 | OpenClaw |
| 沙箱加固反面教材 | OpenClaw CVE 全家桶 + QwenPaw 事后补丁 |
| 安全 guard 分层 | QwenPaw（他们补课补得最勤） |

**总结：** v2 是新生态，v1 的躯壳基本推倒，仅保留（a）用户数据 + 身份、（b）近期刚验过的小砖头（cost_tracker、usage 捕获、打包链）。

---

## 8. 风险与开放问题

### 风险
1. **Phase 1 退出门不达标** → 差异化落空，v2 就只是"v1 但抄 Hermes"。Mitigation：Phase 1 独立评估，不达标就回到"中文 vertical + Hermes 壳"的 fallback。
2. **13 周单人太长** → 期间 Hermes 可能把流式进化也做了。Mitigation：Phase 1 demo 公开发 RFC，抢时间窗。
3. **OS-primitive skills 在 Windows 上 fork 受限** → 用 subprocess + file descriptor 传递模拟，或上 Daytona/Modal backend。
4. **Tool-Call IR 越抽象越难用** → 留一个 "raw passthrough" 逃生口。

### 开放问题
- v2 是新 repo 还是同 repo `v2/` 子目录？（建议：同 repo 新分支，到 Phase 4 后合入 main，v1 打 archive tag）
- 是否保留"基因/技能"二元划分？还是统一为"可演化资产"？
- 流式观察器的事件是否要对用户可见？（至少给 debug view）

---

## 9. 开放 & 兼容 不变量（v2 第一日就必须满足）

面向开发者、面向用户、面向生态的硬约束。

### 接口化（所有横切）
| 接口 | 默认实现 | 替换方式 |
|---|---|---|
| `LLMProvider` | Anthropic、OpenAI、OpenAI-compat | 注册 entry-point，支持任意 compat API |
| `ToolCallIR → Provider` 翻译器 | Anthropic-native、OpenAI-tool-shape、OpenAI JSON-schema | 加一个 translator 就多一家 |
| `MemoryProvider` | sqlite-vec（分层） | Chroma / pgvector / Honcho-compat 随时换 |
| `ChannelAdapter` | WS、Slack、Discord、Telegram、飞书、企业微信、QQ | 注册即生效 |
| `ToolProvider` | built-in + MCP bridge | 任意 MCP server 即插 |
| `SkillRuntime` | 本地进程 | Docker / SSH / Daytona / Modal |
| `Grader` | ground-truth checker | 插自定义领域 grader |
| `Scheduler` | 在线流式 evolution scheduler | 可换回传统 pipeline（对照实验用） |

### 协议兼容
- **MCP** 作为一等公民（tools + resources + prompts）
- **ACP**（Anthropic-compat API）原生支持
- **OpenAI-compat API** 原生支持
- **Anthropic tool-use block** 和 **OpenAI tool_calls** 双向转换无损

### 开放
- 事件 schema 在 `docs/EVENTS.md` 正式 semver，破坏性变更进 CHANGELOG
- Plugin SDK（`xmclaw-plugin-sdk`）独立发包
- `awesome-xmclaw` 社区仓库（空也先开）
- 最小可用样板 plugin（channel / tool / memory / skill 各一个），开箱可 clone

### 跨平台
- Windows / macOS / Linux 三平台 conformance 测试 nightly 跑
- 任何"只在 Windows 上能用"的路径 = blocker

---

## 10. 决策需求（需用户点头后才往下走）

1. 同意阶段切分 + 13 周预估？
2. Phase 1 是否接受作为 go/no-go 门（不达标就考虑 fallback 路径）？
3. Commodity 照抄 Hermes 为主、QwenPaw/OpenClaw 补充 —— 同意？
4. 新分支 `v2-rewrite`、v1 在 `main` 上仅修阻塞 bug —— 同意？
5. 同模型对照 bench（anti-req #11）作为 release gate —— 接受？
6. "进化必须带可验证分数提升证据"（anti-req #12）作为 CI 硬门 —— 接受？
7. 前端从零重构、老 `web/` 完全弃 —— 确认？

---

## 11. 交付现状（2026-04-23）

### 11.1 阶段完成度

| Phase | 目标 | 状态 | 佐证 commit |
|---|---|---|---|
| 1.1 | 事件总线 + IR + 模拟 bandit | ✅ | `418a230` |
| 1.2 | 真 Anthropic provider + domain grader | ✅ | `13d7338` |
| 1.3 | 离线学习曲线 + 真 LLM live bench | ✅ **1.12× on MiniMax** | 同 1.2 |
| 2.0 | OpenAI provider + 跨 provider 翻译器一致性 | ✅ | `a7d34e2` |
| 2.1 | SqliteVecMemory (分层 + 语义) | ✅ | `815162a` |
| 2.2 | Anti-req #11 provider non-interference 门 | ✅ | `5527866` |
| 2.3 | WS ChannelAdapter + 参数化 conformance | ✅ | `158c1e3` |
| 2.4 | 跨 session 记忆（seed 加速下一 session 收敛）| ✅ | `93b1cbb` |
| 2.5 | BuiltinTools + anti-req #1 端到端证据 | ✅ | `3c588c1` |
| 2.6 | 真 LLM tool-aware agent loop | ✅ **100% tool-firing on MiniMax** | `426f994` |
| 3.1 | SkillRegistry + promote/rollback + 审计 | ✅ | `a698d8e` |
| 3.2 | LocalSkillRuntime + CPU timeout + 矩阵 | ✅ | `6c9f346` |
| 3.3 | EvolutionController（mock 1.22×）| ✅ | `22ce315` |
| 3.4 | 进程隔离 runtime（subprocess）| 🟡 落地但非 default — `providers/runtime/process.py` 存在；prod 默认仍是 `LocalSkillRuntime`（Epic #3）|  |
| 3.5 | 真 LLM 自治演化循环 | ✅ **1.18× on MiniMax** | `3f44d6d` |
| 4.0 | Daemon 集成：FastAPI + WS `/agent/v2/{session_id}` + pairing token + event replay | ✅ | `bb3136d` |
| 4.1 | v2 Web UI（`xmclaw/daemon/static/`）+ CLI `xmclaw start/chat/doctor` | ✅ | `fd1d5a1` |
| 4.2 | Strangler-fig：v1 `agent_loop.py` / `evolution/*` / `genes/*` / `task_classifier.py` / 旧 daemon / 旧 UI 删除 | ✅ |  |
| 4.3 | v2-rewrite → main 合并 | ✅ | `fd1d5a1` |
| 5–6 | 热重载 / 发布流水线 v2 化 | 🟡 进行中（Epic #6 / Epic #2）|  |

### 11.2 Anti-requirement 记分卡

| # | Anti-req | 代码证据 | 测试类型 |
|---|---|---|---|
| 1 | 不信任文本 tool call | 翻译器层 ✅ + grader 层 ✅ + live agent loop ✅ (100% 真调用) | 三层 |
| 2 | 非 FTS5-only 记忆 | SqliteVecMemory 分层 + 语义 + surface scan 禁止 auto-inject | Unit + test_memory_provider_has_no_auto_inject |
| 3 | 翻译器不易碎 | Anthropic + OpenAI 各 18/11 个拒绝路径；跨 provider 矩阵 | Conformance matrix |
| 4 | 不让 LLM 自评 | HonestGrader LLM 权重硬卡 0.20，weight sum assert at import | Unit + integration |
| 5 | 技能可回滚 | SkillRegistry 版本化 + append-only 历史 | Unit + integration |
| 6 | 成本硬熔断 | `utils.cost` + BudgetExceeded；独立实现，无 v1 耦合 | Unit（硬 cap e2e 测试随 daemon integration bench 落地）|
| 7 | Channel CI parity | WS adapter + 参数化 conformance matrix (N=1) | Conformance |
| 8 | 设备绑定 | Pairing token 共享秘密：0600 文件、query + header 接受、constant-time compare、`close(4401)` 拒绝、crash-safe | Unit + integration |
| 9 | Session lifecycle | Session lifecycle stub + 跨 session 测试 | Integration |
| 10 | 多后端同构 | Runtime conformance matrix (N=1) | Conformance |
| **11** | **同模型 bench gate** | **provider_noninterference conformance (14 tests)** | **Conformance** |
| **12** | **进化带证据** | **Registry + Controller 双层 "no evidence no promote"** | **Unit + integration + live** |
| **13** | **接口化 + plugin** | 7 个 provider ABC + import-direction CI 门 | **CI + import surface** |
| **14** | **跨协议 + 跨平台** | Anthropic-native + OpenAI-tool 双路；CI matrix 跑 Windows / macOS / Linux on every push | **CI matrix + translator conformance** |

### 11.3 测试计数

```
总 v2 测试数量:     410 passing (unit + integration + conformance + offline bench)
                    on Windows / macOS / Linux CI matrix
Live bench runs 通过:  3 / 3 (XMC_ANTHROPIC_API_KEY 设置时)
  ├─ Phase 1.3 learning curve:          1.119× (gate ≥ 1.05×)
  ├─ Phase 2.6 tool-aware learning:     100% tool-firing (gate ≥ 80%)
  └─ Phase 3.5 autonomous evolution:    1.183× (session 1 vs session 2)
CI 门:                全绿
  ├─ import-direction check（含 utils self-containment 规则）
  ├─ smart-gate（PR）/ full suite（push-to-main）
  └─ v2 ping smoke
```

### 11.4 v2-rewrite 分支 commit 弧

```
418a230  Phase 1.1 HonestGrader + OnlineScheduler
13d7338  Phase 1.2 Anthropic provider + domain grader
5527866  Phase 2.2 anti-req #11 release gate
815162a  Phase 2.1 sqlite-vec memory
a7d34e2  Phase 2.0 OpenAI provider + cross-provider conformance
158c1e3  Phase 2.3 WS channel + conformance
93b1cbb  Phase 2.4 cross-session memory integration
3c588c1  Phase 2.5 BuiltinTools + anti-req #1 e2e
426f994  Phase 2.6 tool-aware live bench  (100% tool-firing on MiniMax)
a698d8e  Phase 3.1 SkillRegistry + versioning
6c9f346  Phase 3.2 LocalSkillRuntime + conformance matrix
22ce315  Phase 3.3 EvolutionController (mock 1.22×)
3f44d6d  Phase 3.5 autonomous evolution LIVE  (1.18× on MiniMax)
55eeb56  Phase 4.9 MCP bridge (JSON-RPC stdio)
777abaf  Phase 4 v1 wipe + flat CLI
bb3136d  Phase 4 event-replay on reconnect + todos + timeline + i18n UI
fd1d5a1  Merge v2-rewrite → main (UI overhaul, event replay, todo tools)
```

---

## 12. 合并回 main 的决策点（已完成）

**历史：** `v2-rewrite` 分支从 `main` 切出、独立推进到 Phase 4，经 PR `fd1d5a1 Merge v2-rewrite — UI overhaul, event-replay, todo tools, ultrathink, i18n` 合回 `main`。v1 strangler-fig 清理同期完成——`xmclaw/genes/*` / `xmclaw/evolution/*` / `xmclaw/core/agent_loop.py` / `task_classifier.py` / 旧 daemon / 旧 Web UI 全删。

**当前状态：** `main` 是 v2-only tree。对外交付版本（`xmclaw.exe` 安装器、PyPI `xmclaw` 包）直接构建自 `main`。`v2-rewrite` 分支已无维护需要。

该节保留为历史记录。后续决策点见 [DEV_ROADMAP.md](DEV_ROADMAP.md) Epic 列表。
