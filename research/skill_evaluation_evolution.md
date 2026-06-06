# Agent 技能系统「评估与进化」层深度调研

> 调研日期：2026-06-06  
> 调研范围：GEPA / DSPy / Self-evolving agents survey / LLM-as-judge 替代方案 / 2025-2026 最新进展  
> 目标读者：XMclaw 进化引擎维护者  
> 前置阅读：`docs/audit/SKILL_SYSTEM_SOTA_RESEARCH_2026.md`

---

## 0. 执行摘要

XMclaw 的评估-进化闭环（HonestGrader → ReflectiveMutator → VariantSelector UCB1 → EvolutionController 四阈值门）在**评估诚实性**维度上领先行业头部（拒绝 LLM-as-judge，用确定性 ground-truth 检查）。2025-2026 年的新进展集中在三个方向：

1. **GEPA 成为 DSPy 一等公民**：`dspy.GEPA` 已内置，样本效率比 GRPO 高 35×；
2. **RL-in-the-loop 技能进化**：SkillRL / ReSkill / SAGE 把技能库直接嵌入 GRPO 训练循环，不再离线；
3. **评估可靠性被 survey 列为核心未决问题**：arXiv:2507.21046 明确将「评估可靠性 + 安全」列为自进化 agent 的最大瓶颈，XMclaw 的多信号 Iron Rule #1 恰好对准这一痛点。

**一句话建议**：XMclaw 的进化主轴（评估→变异→选择→晋升）是护城河，别动；可轻量嫁接的方向是 (a) GEPA 的 meta-reflection 全局记忆，(b) RL-in-the-loop 的 skill-policy 共进化，(c) 评估层补全 CrossJudgeSignal 的跨模型 plumbing。

---

## 1. GEPA（反思式 Prompt 进化）

### 1.1 论文与出处

- **GEPA: Reflective Prompt Evolution Can Outperform Reinforcement Learning**  
  Agrawal et al., ICLR 2026 (Oral), arXiv:2507.19457  
  核心主张：将「反思」与「进化」结合，用自然语言反思计划（reflection plan）驱动 prompt 变异，比标量奖励的 RL（GRPO）样本效率高 35 倍。

- **GEPA 三步骤循环**（每轮优化迭代）：
  1. **选择 + 评估**：维护一个 Pareto archive，记录哪些 prompt 在哪些 problem instance 上获胜；随机采样非支配候选，在 mini-batch 上评估；
  2. **更新 meta-reflection**：`R_t := MetaReflect(H_t; M_critic)`，critic 看代表性高/低表现样本，提炼成功模式和失败模式，生成可操作的 prompt 级指导；
  3. **进化 prompt**：对步骤 1 评估的 prompt 做一步 textual-gradient 进化，仅当 offspring 得分更高时才接受，并追加到候选集（不删除原 prompt）。

### 1.2 真实源码

- **官方实现**：`github.com/allthingssecurity/GEPA`（PyPI: `pip install gepa`）  
  关键类：`GEPA`（主编排器）、`CandidateManager`、`Reflector`、`Mutator`、`ParetoSelector`。

- **DSPy 内置**：`dspy.GEPA`（`dspy.ai` 文档 2025-07 上线）  
  使用方式：
  ```python
  import dspy
  optimizer = dspy.GEPA(metric=accuracy, auto="medium")
  compiled = optimizer.compile(module, trainset=examples, valset=val)
  # compiled.predict.signature.instructions 即为进化后的 prompt
  ```

- **GEPA 核心代码片段**（来自官方 repo 的 `gepa/core.py` 概念）：
  ```python
  class GEPA:
      def evolve(self, population, tasks):
          for generation in range(max_generations):
              # 1. 评估
              scores = self.evaluator.evaluate(population, tasks)
              # 2. Pareto 选择
              front = self.pareto_selector.select(population, scores)
              # 3. 反思
              reflection = self.reflector.reflect(front, scores)
              # 4. 变异
              offspring = self.mutator.mutate(front, reflection)
              population = front + offspring
          return best_candidate
  ```

### 1.3 与 XMclaw `ReflectiveMutator` 的对比

| 维度 | GEPA (论文 + 官方实现) | XMclaw `ReflectiveMutator` |
|---|---|---|
| **反思深度** | 全局 meta-reflection `R_t` 跨迭代累积，critic 看高/低表现样本提炼模式 | 单轮 `(head_source, recent_failures) → ≤3 候选`，无跨轮 meta-reflection 记忆 |
| **Pareto 选择** | 实例级 Pareto archive，保留「在哪些 instance 上赢」的互补策略 | `ParetoFrontier` 按 `(skill_id, context_signature)` 分桶，保留 per-context 最优版本 |
| **样本效率** | 35× fewer rollouts than GRPO | 未做系统级 benchmark；依赖 UCB1 在线探索 |
| **confidence 封顶** | 无显式封顶，依赖评估 metric | **confidence ≤ 0.6**（Iron Rule：无运行时证据不准自夸） |
| **变异来源** | 基于 textual gradient + reflection plan | 基于 LLM 单轮自批判 + JSON 解析 |
| **集成度** | DSPy 一等公民，可 `dspy.GEPA()` 直接调用 | 已集成 DSPy `SkillMutator`（`mutator.py` 走 `GEPA().compile()`），但 `ReflectiveMutator` 是独立无 DSPy 路径 |

**差距与建议**：
- 🟡 XMclaw 的 `ReflectiveMutator` 缺 GEPA 的**跨迭代 meta-reflection 记忆**。建议：在 `reflective_mutator.py` 中增加一个 `MetaReflectionStore`，把每轮 reflection_summary 写入持久化记忆（如 `~/.xmclaw/meta_reflections/<skill_id>.json`），后续轮次 prompt 中携带最近 N 条 reflection，避免重复犯同样的错。
- 🟢 XMclaw 的 `ParetoFrontier` 已经是 per-context 的，比 GEPA 的全局 Pareto archive 更细粒度，无需改。
- 🟢 XMclaw 的 confidence 封顶体现了更强的诚实原则，GEPA 没有这一约束。

---

## 2. DSPy（编译/优化 LM 管线）

### 2.1 论文与出处

- **DSPy: Compiling Declarative Language Model Calls into Self-Improving Pipelines**  
  Khattab et al., NeurIPS 2023 / ICLR 2024, arXiv:2310.03714  
  核心主张：把 LM pipeline 抽象为「文本变换图」（text transformation graph），用声明式模块替代脆弱的 prompt 模板，通过编译器自动优化 prompts 和 weights。

- **关键数据**：编译后 GPT-3.5 的 pipeline 比标准 few-shot 高 25-65%；llama2-13b-chat 比专家写的 demonstration 高 16-40%；770M T5 可与 GPT-3.5 专家 prompt chain 竞争。

### 2.2 最新进展（2025-2026）

| 时间 | 进展 | 说明 |
|---|---|---|
| 2025-07 | `dspy.GEPA` 内置 | 反思式 prompt 优化成为 DSPy 官方 optimizer |
| 2025-07 | MIPROv2 | 优化 instructions + demonstrations，比 MIPROv1 更稳定 |
| 2025-12 | FlashEvolve (arXiv:2605.08520) | 异步 worker + queue 加速进化，GEPA 吞吐量 3.5× |
| 2026-01 | OpenClaw / KISS Sorcar | DSPy 团队的新方向：递归语言模型 + 自举编译 |

### 2.3 真实源码

- **DSPy 主仓库**：`github.com/stanfordnlp/dspy`（~20k stars）
- **XMclaw 中的 DSPy 集成**（`xmclaw/core/evolution/mutator.py`）：
  ```python
  def _run_dspy_compile(self, *, baseline_text: str, dataset: EvalDataset) -> str:
      dspy = self._dspy
      signature = dspy.Signature("user_msg -> response")
      module = dspy.ChainOfThought(signature)
      module.predict.signature = module.predict.signature.with_instructions(baseline_text)
      
      examples = [dspy.Example(user_msg=ex.task_input, response=ex.expected_behavior)
                  .with_inputs("user_msg") for ex in dataset.train]
      
      def metric(example, prediction, trace=None) -> float:
          text = getattr(prediction, "response", "") or ""
          ex = EvalExample(task_input=example.user_msg, expected_behavior=example.response, baseline_score=0.0)
          return self._fitness_fn(ex, str(text))
      
      # Try GEPA → MIPROv2 → BootstrapFewShot
      for cls_name in ("GEPA", "MIPROv2", "BootstrapFewShot"):
          cls = getattr(dspy, cls_name, None)
          if cls:
              try:
                  optimizer = cls(metric=metric, max_steps=self._iterations)
                  break
              except TypeError:
                  optimizer = cls(metric=metric)
                  break
      
      compiled = optimizer.compile(module, trainset=examples, valset=valset)
      return compiled.predict.signature.instructions  # 进化后的 skill body
  ```

### 2.4 与 XMclaw 的对比

| 维度 | DSPy 生态 | XMclaw `SkillMutator` |
|---|---|---|
| **优化目标** | 最大化任意 metric（通常是 accuracy/F1） | 最大化 `xmclaw_fitness`（HonestGrader-shaped 代理） |
| **fitness 来源** | 外部标注或 LLM-as-judge | **自 grading** — 从 `events.db` 的 `GRADER_VERDICT` 构建数据集，用与运行时相同的 ground-truth 检查 |
| **fallback 链** | GEPA → MIPROv2 → BootstrapFewShot | 相同 fallback 链（`mutator.py:251-264`） |
| **依赖管理** | 可选依赖（`dspy-ai` 用户自装） | 相同策略（`_try_import_dspy` 懒加载，缺失则 no-op） |
| **约束层** | DSPy 本身无约束 | XMclaw 在 DSPy 之后加 `validate_candidate`（size/growth/retain_ratio/structure） |

**差距与建议**：
- 🟢 XMclaw 的 fitness 函数是**自 grading**（从自己的 bus 事件日志构建数据集），这是比 DSPy 默认做法更强的差异化。保持。
- 🟡 DSPy 2025-12 的 **FlashEvolve**（异步加速）对 XMclaw 有参考价值。当前 `SkillMutator.mutate` 用 `asyncio.to_thread` 把同步 DSPy 调用放进线程池；若进化吞吐量成为瓶颈，可借鉴 FlashEvolve 的 worker-queue 模型把 compile 阶段异步化。
- 🟡 DSPy 的 **MIPROv2** 在 demonstration 优化上比 GEPA 更稳定；XMclaw 的 fallback 链已包含 MIPROv2，但默认优先 GEPA。若观察到 GEPA 在弱模型上不稳定，可调换优先级。

---

## 3. Self-evolving Agents Survey

### 3.1 论文与出处

- **A Survey of Self-Evolving Agents: On Path to Artificial Super Intelligence**  
  Gao et al., arXiv:2507.21046, 2025  
  按 What/When/How/Where 四个维度系统分类，GitHub: `github.com/CharlesQ9/Self-Evolving-Agents`

- **A Comprehensive Survey of Self-Evolving AI Agents: A New Paradigm Bridging Foundation Models and Lifelong Agentic Systems**  
  Fang et al., arXiv:2508.07407, 2025

### 3.2 对「评估可靠性 + 安全」的核心分析

arXiv:2507.21046 §7 明确指出：

> "Evaluating self-evolving agents presents a unique set of challenges... their evaluation must capture not only immediate task success but also crucial aspects such as adaptation over time, knowledge accumulation and retention, long-term generalization, and the ability to transfer learned skills across sequential or novel tasks, all while mitigating catastrophic forgetting."

该 survey 把评估目标分为五类：
1. **Adaptivity** — 随时间适应新任务的能力
2. **Retention** — 知识累积与保留（防灾难性遗忘）
3. **Generalization** — 长期泛化与跨任务迁移
4. **Safety** — 进化过程中的安全约束
5. **Efficiency** — 进化效率（样本/计算/时间成本）

**评估范式**从静态评估（single-shot）→ 短期适应性 → 长期终身学习评估（long-horizon lifelong learning）。

### 3.3 与 XMclaw 的对比

| 维度 | Survey 头部做法 | XMclaw 现状 |
|---|---|---|
| **评估信号** | 多数用 LLM-as-judge；少数用 env self-verification（Voyager/Minecraft） | **HonestGrader 确定性 ground-truth**（`check_ran`/`returned`/`type_matched`/`side_effect`）+ 独立信号层（UserFollowup / HoldoutTest / CrossJudge） |
| **晋升门控** | 多数按 benchmark 分数直接 promote；ADAS 维护 archive | **四阈值门**（plays≥min_plays / mean≥min_mean / gap_over_head / best_minus_second）+ Pareto frontier + **结构性禁止自晋升** |
| **安全约束** | Survey 列为「核心未决问题」 | `constraints.validate_candidate`（size/growth/retain_ratio/structure）+ `EvolutionController` 的 Iron Rule #1/#2 |
| **长期评估** | 尚无成熟方案，多为静态 benchmark | `events.db` 持久化 + UCB1 在线累积，但缺少跨 session 的纵向 retention 指标 |

**差距与建议**：
- 🟢 XMclaw 在**评估诚实性**上领先 survey 中提到的绝大多数系统。survey 明确批评 LLM-as-judge 的可靠性问题，而 XMclaw 的 Iron Rule #1（≥2 独立信号才能晋升）正是对准这一痛点。
- 🟡 **长期 retention / 灾难性遗忘**：survey 强调这是开放难题。XMclaw 的 `ParetoFrontier` 保留 per-context 版本，但缺少显式的「旧版本在新任务上是否退化」的 holdout 检查。建议：在 `HoldoutTestSignal` 中增加跨时间窗口的 regression test（如每月重跑一遍历史 holdout 集）。
- 🟡 **安全约束**：survey 把安全列为自进化的核心未决问题。XMclaw 已有 size/growth/structure 约束，但缺少「技能内容安全」的语义层检查（如 prompt 注入、有害指令）。建议：在 `constraints.py` 的 `required_sections` 之外，增加一个 `safety_scan` 步骤（复用已有的注入扫描器）。

---

## 4. LLM-as-judge 的替代方案

### 4.1 头部系统的评估方法全景

| 方法 | 代表工作 | 优点 | 缺点 |
|---|---|---|---|
| **LLM-as-judge** | MT-Bench, Chatbot Arena, Self-Rewarding | 灵活、可解释、无需标注 | 位置偏见、长度偏见、自我增强、hard task 上接近随机 |
| **环境自验证** | Voyager (Minecraft), AZR (Python 执行), SPICE (检索验证) | 客观、可重放 | 仅适用于有可执行/可检索验证域 |
| **人类反馈** | RLHF, Constitutional AI | 高质量偏好信号 | 昂贵、慢、不可扩展 |
| **多模型投票** | Majority voting, Weighted averaging, Panel discussion | 降低单模型偏见 | 成本高、 disagreement 本身也是信号 |
| **Meta-judging** | Meta-Rewarding (Wu et al., 2025), SRE (Trivedi et al., 2024) | 模型自我改进评估能力 | 训练复杂、分布漂移 |
| **确定性 ground-truth** | XMclaw HonestGrader, VerifierBench | 诚实、不可博弈、可重放 | 需要可验证的副作用声明 |

### 4.2 最新进展（2025-2026）

- **Meta-Rewarding** (Wu et al., 2025)：单模型循环扮演 actor/judge/meta-judge，用 DPO 迭代改进 judge，win rate 从 22.9% → 39.4%。
- **Learning While Evaluating (LWE)** (arXiv:2512.06751)：推理时 meta-prompt 生成 sample-specific 评估 prompt，evaluator 自我反馈更新 meta-prompt，无需额外训练。
- **Confidence-Orchestrated Self-Evolution (COSE)** (arXiv:2605.28010)：显式建模 LLM 反馈的不确定性，低 confidence 反馈被降权，防止错误信号放大。
- **OpenDeepThink** (arXiv:2605.15177)：用 Bradley-Terry 聚合 pairwise LLM 判断，替代 pointwise scoring，更稳定。

### 4.3 与 XMclaw `HonestGrader` 的对比

XMclaw 的评估架构（Sprint 3 Iron Rule #1）是**分层多信号**设计：

```python
# Signal A — 确定性检查（verdict.py + checks.py）
det_score = weighted(check_ran, check_returned, check_type_matched, check_side_effect)

# Signal B — 独立信号（_signals.py）
ind_score = first_fires(UserFollowupSignal, HoldoutTestSignal, CrossJudgeSignal)

# 组合规则
if ind_score is None:
    final = det_score; promote_eligible = False  # Iron Rule #1
else:
    final = 0.6 * det_score + 0.4 * ind_score
    promote_eligible = (det >= 0.6 AND ind >= 0.5)
```

| 维度 | 头部替代方案 | XMclaw HonestGrader |
|---|---|---|
| **核心哲学** | 用 LLM 或人类判断「质量」 | 用可验证的客观信号证明「做过」 |
| **ran 检查** | 多数系统仅检查「没 crash」 | **Sprint 3 收紧**：拒绝 empty/whitespace/fake-success sentinel（"ok"/"done"/"true"） |
| **type 检查** | 通常无 | 声明 `expected_type` 则严格检查；未声明返回 `None`（不白给分） |
| **side effect** | 通常仅检查 fs | 扩展至 `memory://` 和 `bus://` URI 方案 |
| **独立信号** | Meta-judging / 多模型投票 | **UserFollowup**（用户后续反应）+ **HoldoutTest**（注册表检查）+ **CrossJudge**（跨模型 disagreement-as-negative） |
| **promote 门控** | 通常单信号 ≥ threshold | **必须 ≥2 独立信号**，单信号即使分数高也 BLOCK |

**差距与建议**：
- 🟢 **这是 XMclaw 的护城河**。survey 和 Meta-judging 论文都承认 LLM judge 的偏见和噪声是「reward hacking」的根源；XMclaw 的 ground-truth 检查不可博弈。
- 🟡 **CrossJudgeSignal 是 stub**（`_signals.py:436-504`）。论文 arXiv:2505.22960 显示 multi-agent debate ceiling = best single agent，但 **disagreement 确实 correlate with low quality**。XMclaw 的设计（disagreement → score=0.0）是对的，但 plumbing 未接。建议：当系统配置了两个不同 family 的 LLM（如 Claude + GPT），在关键技能调用后触发第二 judge，把 `cross_judge_a`/`cross_judge_b` 写入 event payload。
- 🟡 **Meta-Rewarding 的启示**：XMclaw 不需要训练 judge 模型，但可以把「用户 followup + holdout test」的 disagreement 作为**负面训练信号**反馈给 `ReflectiveMutator` 的 meta-reflection，告诉它「你上次改的 skill 用户不满意」。

---

## 5. 2025-2026 年技能评估和进化的最新方法

### 5.1 在线学习 / 强化学习 / 进化算法

| 论文 | 方法 | 核心创新 | 与 XMclaw 关系 |
|---|---|---|---|
| **SkillRL** (Xia et al., arXiv:2602.08234, 2026) | 递归技能增强 RL | SKILLBANK 层次技能库 + GRPO 联合优化策略和技能库；失败轨迹触发新技能生成 | XMclaw 是离线进化（UCB1 选版本），SkillRL 是在线 RL-in-the-loop。可借鉴：把技能库直接嵌入 agent 的 RL 训练 |
| **EvoSkill** (Alzubi et al., arXiv:2603.02766, 2026) | 失败驱动的技能发现 | Proposer 分析失败 → 诊断原因 → 提出新 skill；Pareto frontier 保留验证集上不退化的技能 | 与 XMclaw `SkillInductor`（`inductor.py`）思路一致，但 EvoSkill 有系统级 benchmark（OfficeQA +7.3%, SealQA +12.1%） |
| **ReSkill** (He et al., arXiv:2606.01619, 2026) | 技能创建与策略优化和解 | GRPO 组内采样做 skill 版本对照；Thompson Sampling 自适应探索；assertion-driven skill creator | 与 XMclaw `VariantSelector` UCB1 类似，但 ReSkill 用 Thompson Sampling 且嵌入 GRPO 循环。可借鉴：把 UCB1 升级为 Thompson Sampling |
| **SkillSmith** (arXiv:2606.01314, 2026) | 技能-工具共进化 | 同时进化技能和工具，解决「失败是工具缺陷还是技能缺陷」的归因问题 | XMclaw 目前技能扁平、工具固定。SkillSmith 的共进化思路可作为中长期方向 |
| **SAGE** (Wang et al., Dec 2025) | Skill Augmented GRPO for self-Evolution | 系统地把技能库纳入 agent RL 训练，AppWorld 上 +8.9% 完成率，-26% 步数，-59% token | 与 SkillRL 同方向，更强调 token 效率 |
| **AutoSkill** (Ye et al., arXiv:2603.01145, 2026) | 经验驱动的终身学习 | 从轨迹中自动发现、评估、整合技能，持续学习 | 与 XMclaw `SkillInductor` + `user_loader` 的终身扫描思路一致 |
| **FlashEvolve** (arXiv:2605.08520, 2026) | 异步进化加速 | worker-queue + artifact-pool versioning + staleness-aware policy，GEPA 吞吐量 3.5× | XMclaw 进化是同步串行（propose → test → promote），FlashEvolve 的异步模型可作为性能优化方向 |

### 5.2 关键趋势总结

1. **从离线到在线**：2025 年前技能进化多为离线（GEPA/DSPy 编译后部署）；2026 年主流是 RL-in-the-loop（SkillRL/ReSkill/SAGE），技能库与策略同步进化。
2. **从单技能到技能-工具共进化**：SkillSmith 指出「只改 skill 不改 tool」会导致虚假增益，归因问题必须联合优化。
3. **从标量奖励到结构化反馈**：GEPA 的 textual feedback、ReSkill 的 assertion-driven creator、EvoSkill 的失败诊断，都比标量分数 richer。
4. **从全局最优到 per-context Pareto**：GEPA 和 EvoSkill 都保留 per-instance / per-task 的 Pareto 前沿，不强制单一全局 winner。XMclaw 的 `ParetoFrontier` 已对齐这一趋势。

### 5.3 与 XMclaw 的对比与建议

| 趋势 | XMclaw 现状 | 建议 |
|---|---|---|
| **RL-in-the-loop** | 进化与运行时分离（UCB1 在线选版本，但策略本身不 RL 训练） | 🟡 中长期：探索把 `SkillResult` 的 reward 信号接入 GRPO 训练循环（若 XMclaw 未来有 policy 模型） |
| **技能-工具共进化** | 工具固定，技能扁平 | 🟡 中期：当工具层（MCP server / builtin tools）出现可配置参数时，让 `ReflectiveMutator` 同时提议 tool 参数调整 |
| **Thompson Sampling** | UCB1 (`VariantSelector`) | 🟡 轻量升级：UCB1 → Thompson Sampling（已有 posterior 估计，只需加 Gaussian 采样） |
| **异步进化** | 同步串行 | 🟢 低优先：当前进化吞吐量不是瓶颈（个人助理场景），FlashEvolve 适合高并发 SaaS |
| **失败诊断结构化** | `recent_failures` 是原始 event list | 🟡 建议：在 `reflective_mutator.py` 的 prompt 前加一层 `FailureDiagnoser`，用 LLM 把原始失败提炼成结构化诊断（如「缺少类型检查」「副作用未声明」），再喂给 mutator |

---

## 6. 总结：给 XMclaw 的优先级建议

### 6.1 保持不动（护城河）

1. **HonestGrader 的多信号架构** — Iron Rule #1（≥2 独立信号才能晋升）是差异化核心，survey 和行业都承认 LLM-as-judge 不可靠。
2. **EvolutionController 的四阈值门 + Pareto frontier** — 比头部更克制，防止噪声晋升。
3. **ReflectiveMutator 的 confidence 封顶（0.6）** — 体现诚实原则，GEPA 没有这一约束。

### 6.2 轻量增强（高 ROI）

1. **补全 CrossJudgeSignal plumbing**（`blocked_by_iron_rule_1` 已留好接口）  
   当系统配置多模型时，把第二 judge 的分数写入 event payload，让 disagreement-as-negative 真正生效。

2. **ReflectiveMutator 增加 meta-reflection 记忆**  
   每轮 reflection_summary 持久化，后续轮次携带最近 N 条，避免重复犯错。代码位置：`xmclaw/core/evolution/reflective_mutator.py` + `xmclaw/core/memory/`。

3. **UCB1 → Thompson Sampling**（`VariantSelector`）  
   已有 plays + mean，只需把 `arm.mean + exploration_c * sqrt(log_total / plays)` 替换为从 `N(mean, 1/plays)` 采样。更自然处理不确定性。

4. **失败诊断结构化**  
   在 `_build_prompt` 前加 `FailureDiagnoser`，把 `recent_failures` 的原始 event 提炼成「失败模式标签」（如 `type_mismatch`, `side_effect_missing`, `fake_success`），让 mutator 的 reflection 更有针对性。

### 6.3 中长期探索

1. **RL-in-the-loop 技能-策略共进化**  
   若 XMclaw 未来引入可训练的 policy 模型（而非纯 prompt 工程），参考 ReSkill/SkillRL 把技能库嵌入 GRPO 训练循环。

2. **技能-工具共进化**  
   当 MCP tool 参数可配置时，让进化循环同时优化 skill 描述和 tool 调用模式（SkillSmith 方向）。

3. **长期 retention 评估**  
   每月重跑历史 holdout 集，检测「新版本在旧任务上是否退化」，补全 survey 指出的 catastrophic forgetting 监控缺口。

---

## 7. 出处索引

| 论文/项目 | 链接 |
|---|---|
| GEPA (ICLR 2026 Oral) | arXiv:2507.19457 / `github.com/allthingssecurity/GEPA` |
| DSPy (NeurIPS 2023 / ICLR 2024) | arXiv:2310.03714 / `github.com/stanfordnlp/dspy` / `dspy.ai` |
| Self-evolving Agents Survey (What/When/How/Where) | arXiv:2507.21046 / `github.com/CharlesQ9/Self-Evolving-Agents` |
| Self-evolving AI Agents Survey (Comprehensive) | arXiv:2508.07407 |
| SkillRL | arXiv:2602.08234 |
| EvoSkill | arXiv:2603.02766 |
| ReSkill | arXiv:2606.01619 |
| SkillSmith | arXiv:2606.01314 |
| FlashEvolve | arXiv:2605.08520 |
| Meta-Rewarding | Wu et al., 2025 |
| Learning While Evaluating (LWE) | arXiv:2512.06751 |
| Confidence-Orchestrated Self-Evolution (COSE) | arXiv:2605.28010 |
| OpenDeepThink | arXiv:2605.15177 |
| SAGE (Skill Augmented GRPO) | Wang et al., Dec 2025 |
| AutoSkill | arXiv:2603.01145 |
| XMclaw HonestGrader | `xmclaw/core/grader/verdict.py` + `checks.py` + `_signals.py` |
| XMclaw ReflectiveMutator | `xmclaw/core/evolution/reflective_mutator.py` |
| XMclaw SkillMutator (DSPy) | `xmclaw/core/evolution/mutator.py` |
| XMclaw VariantSelector | `xmclaw/skills/variant_selector.py` |
| XMclaw EvolutionController | `xmclaw/core/evolution/controller.py` |
| XMclaw ParetoFrontier | `xmclaw/core/evolution/pareto_frontier.py` |
| XMclaw Constraints | `xmclaw/core/evolution/constraints.py` |
| XMclaw Dataset Builder | `xmclaw/core/evolution/dataset.py` |

---

> **复核声明**：本调研基于 2026-06-06 可获取的公开论文、源码仓库和 XMclaw 真实代码。所有对比均引用具体文件路径和代码片段，非泛泛而谈。
