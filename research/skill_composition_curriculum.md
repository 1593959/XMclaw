# Agent 技能系统「组合与课程」层调研报告

> 研究员：组合课程  
> 日期：2026-06-06  
> 范围：技能组合（Composition）、自动课程（Curriculum）、Meta-Agent 组合、技能链/图、2025-2026 最新进展  
> 要求：每点含「论文/出处 + 真实源码/代码片段 + 与 XMclaw 对比」

---

## 目录

1. [Voyager 技能组合：代码即技能，召回即拼接](#1-voyager-技能组合)
2. [自动课程（Automatic Curriculum）](#2-自动课程)
3. [Meta-Agent 组合：ADAS 与代码空间搜索](#3-meta-agent-组合)
4. [技能链与技能图：从参数空间到执行图](#4-技能链与技能图)
5. [2025-2026 最新进展](#5-2025-2026-最新进展)
6. [XMclaw 差距总览与建议优先级](#6-xmclaw-差距总览与建议优先级)

---

## 1. Voyager 技能组合

### 1.1 论文与出处

- **Voyager: An Open-Ended Embodied Agent with Large Language Models**  
  Wang et al., NVIDIA / Caltech / UT Austin / Stanford / ASU, 2023  
  arXiv:2305.16291 · [官网](https://voyager.minedojo.org/) · [源码](https://github.com/MineDojo/Voyager)

### 1.2 核心机制

Voyager 把技能表示为**可执行 JavaScript 代码**（而非自然语言指令）。每个技能是一个 `async function`，可在 Mineflayer 环境中直接运行。技能库由三部分组成：

- `skill/code/` — 可执行 `.js` 文件
- `skill/description/` — 自然语言描述 `.txt`
- `skill/skills.json` — 元数据索引
- `skill/vectordb/` — Chroma 向量数据库（description 的 embedding）

### 1.3 真实源码片段

**SkillManager.add_new_skill**（`voyager/agents/skill.py`）— 成功轨迹→新技能：

```python
def add_new_skill(self, info):
    program_name = info["program_name"]
    program_code = info["program_code"]
    # 1. 用 LLM 从代码+函数名生成自然语言描述
    skill_description = self.generate_skill_description(
        program_name, program_code
    )
    # 2. 存入 skills.json
    self.skills[program_name] = {
        "code": program_code,
        "description": skill_description,
    }
    # 3. 描述向量化后入库
    self.vectordb.add_texts(
        texts=[skill_description],
        metadatas=[{"name": program_name}],
        ids=[program_name],
    )
```

**SkillManager.retrieve_skills** — 语义召回后返回**完整代码**：

```python
def retrieve_skills(self, query):
    k = min(self.vectordb._collection.count(), self.retrieval_top_k)
    if k == 0:
        return []
    docs_and_scores = self.vectordb.similarity_search_with_score(query, k=k)
    skills = []
    for doc, _ in docs_and_scores:
        # 返回的是可执行代码，不是描述
        skills.append(self.skills[doc.metadata["name"]]["code"])
    return skills
```

**Voyager 主循环**（`voyager/voyager.py`）— 组合发生在执行层：

```python
# reset() 时把召回的技能代码拼进 Action Agent 的系统消息
skills = self.skill_manager.retrieve_skills(query=self.context)
system_message = self.action_agent.render_system_message(skills=skills)

# step() 时环境执行携带全部已召回技能代码
events = self.env.step(
    code,
    programs=self.skill_manager.programs,  # <-- 所有已学技能都注入
)
```

**generate_skill_description** 的 prompt（`voyager/prompts/skill.txt`）要求：

> "Do not mention the function name in the skill description. Try to summarize what the function does in 6 sentences or less."

### 1.4 组合的本质

Voyager 的「组合」不是显式的 "A 调用 B"，而是**代码层面的累积**：

1. 新任务生成代码时，Action Agent 的 system prompt 里已经包含了所有相关技能的完整源码；
2. GPT-4 可以在新生成的 `program_code` 里直接调用已有技能函数；
3. 环境执行时把 `programs`（全部技能库）一并注入 Mineflayer 上下文。

因此复杂技能是**由 LLM 在代码生成阶段自发组合**简单技能而成，而非预先定义的组合图。

### 1.5 与 XMclaw 的对比

| 维度 | Voyager | XMclaw |
|------|---------|--------|
| 技能表示 | 可执行 JS 代码 | `Skill` ABC + `SkillManifest`；SKILL.md 为自然语言指令 |
| 组合方式 | 代码级累积（LLM 在生成时调用已有函数） | **基本扁平**；`SkillResult` 之间无显式组合原语 |
| 召回内容 | 返回**完整源码**拼进 prompt | 返回 `Skill` 对象，由 agent 通过 tool 调用执行 |
| 新技能归纳 | ✅ 成功轨迹→LLM 生成描述→自动入库 | ✅ 已落地（`SkillInductor`/`inductor.py`），但产出为 SKILL.md 而非可执行代码 |
| 检索机制 | Chroma embedding top-k | `SkillSemanticIndex` embedding + token-overlap hybrid |

**差距**：XMclaw 已具备「轨迹→技能」归纳能力（`inductor.py`），但技能体是**自然语言指令**（SKILL.md），不是 Voyager 式的**可执行代码**。这意味着 XMclaw 的技能组合必须依赖 LLM 在每一轮重新理解指令并调用工具，而 Voyager 的技能一旦被召回就是确定性执行。建议：对高频、稳定的技能链，探索生成**确定性子程序**（如 Python 函数或 bash 脚本）作为技能体，降低 LLM 重复推理的 token 开销。

---

## 2. 自动课程（Automatic Curriculum）

### 2.1 Voyager 的自动课程

**论文**：同上（arXiv:2305.16291）  
**源码**：`voyager/agents/curriculum.py`

**CurriculumAgent.propose_next_task** 的核心逻辑：

```python
def propose_next_task(self, events, chest_observation, max_retries=5):
    # 根据当前状态（inventory、biome、completed_tasks、failed_tasks）
    # 由 GPT-4 提出下一个最大化探索的任务
    #  overarching goal: "discover as many diverse things as possible"
    ...
```

课程设计原则（in-context novelty search）：

- 考虑 agent 当前技能水平（`completed_tasks` 数量决定难度）；
- 考虑世界状态（biome、time、inventory）；
- 失败任务会进入 `failed_tasks`，后续课程会回避或简化；
- 成功任务会解锁更复杂的后续任务（类似游戏科技树）。

**Voyager 主循环中的课程更新**：

```python
while True:
    task, context = self.curriculum_agent.propose_next_task(
        events=self.last_events,
        chest_observation=...,
    )
    messages, reward, done, info = self.rollout(task=task, context=context)
    if info["success"]:
        self.skill_manager.add_new_skill(info)          # 成功→学技能
    self.curriculum_agent.update_exploration_progress(info)  # 更新课程进度
```

### 2.2 EvoCurr：LLM 驱动的自主课程 + 行为代码生成

- **论文**：EvoCurr: Self-evolving Curriculum with Behavior Code Generation for Complex Decision-making  
  arXiv:2508.09586, 2025-08

**架构**：三阶段闭环

1. **Curriculum Designer** — 根据当前能力和上一任务结果，生成渐进复杂度的训练任务；
2. **Behavior Coder** — Planner→Coder→Critic 循环，把课程翻译成可执行决策树代码（python-sc2）；
3. **Environment Evaluation** — 胜率、决策准确率反馈回课程设计师，调整难度。

关键发现：

- 直接生成最终任务的代码成功率低；
- 通过课程渐进（5 海军陆战队员→10 海军陆战队员+医疗运输机→…→完整部队），最终任务胜率可达 **100%**；
- 课程会根据胜率阈值自适应：超过阈值则增加复杂度，未达标则回退简化。

### 2.3 SEAgent：自主技能发现 + 课程生成

- **论文**：Agent Skills for Large Language Models: Architecture, Acquisition, Security, and the Path Forward（综述）  
  arXiv:2602.12430, 2026-02
- **SEAgent 出处**：Xu et al., 2026

SEAgent 引入：

- **World State Model** — 逐步轨迹评估；
- **Curriculum Generator** — 从持续更新的软件指南记忆中产生越来越复杂的任务；
- **Specialist-to-Generalist Training** — 把领域专用 agent 的洞察整合到统一模型。

在 OSWorld 的 5 个新软件环境中，成功率从 **11.3% → 34.5%**（+23.2pp）。

### 2.4 与 XMclaw 的对比

| 维度 | Voyager / EvoCurr / SEAgent | XMclaw |
|------|------------------------------|--------|
| 课程驱动者 | **主动探索** — 根据当前能力提议「下一个该学什么」 | **反应式修补** — grader 失败驱动变异，无探索课程 |
| 课程来源 | 环境状态 + 已完成/失败任务历史 | 无；`AutonomyPolicy` 有 `curriculum_edit` 但无自动生成 |
| 难度调节 | 自适应：成功则升难，失败则降难 | 无难度梯度；所有变异在同一技能上随机试探 |
| 目标 | 最大化探索 / 解锁新能力 | 最大化 grader 分数 / 修复失败 |

**差距**：XMclaw 没有「自动课程」层。`AutonomyPolicy`（`xmclaw/cognition/autonomy.py`）把 `curriculum_edit` 标记为 low-risk action，但**没有课程生成器**来主动提议「你还缺哪类能力、去补一个」。当前进化是「哪里漏补哪里」，不是「按能力图谱系统性成长」。

**建议**：

1. 短期：在 `SkillInductor` 成功归纳新技能后，由 LLM 评估「该技能解锁了哪些下游能力」，生成候选课程条目；
2. 中期：维护一个**能力依赖图**（如「git-commit → 需要 commit-message-storyteller → 需要 conventional-commit」），课程按拓扑序提议；
3. 长期：引入 EvoCurr 式的自适应难度——若某类任务连续失败，回退到更简单的子技能组合。

---

## 3. Meta-Agent 组合

### 3.1 ADAS：Automated Design of Agentic Systems

- **论文**：Automated Design of Agentic Systems  
  Hu et al., 2024-08 · arXiv:2408.08435 · **ICLR 2025**  
  [源码](https://github.com/ShengranHu/ADAS) · [社区复现](https://github.com/RayZhhh/adas-ad)

**核心思想**：把 agent 定义在**代码空间**（Python），用一个 meta-agent 迭代地编程新 agent。

**Meta Agent Search 算法**：

```
Archive = 初始 agent 库（如 Chain-of-Thought, Self-Refine）
for t in 1..T:
    context = select(Archive)          # 从档案中选 stepping-stones
    new_agent = meta_agent_program(context)  # GPT-4 写新 agent 的 forward()
    score = evaluate(new_agent)      # 在验证集上跑
    Archive.add(new_agent, score)    # 加入档案，供下一轮使用
```

**关键设计**：

- 搜索空间 = Python 代码（图灵完备，可表达任意 prompt / tool / workflow 组合）；
- Meta-agent 只写 `forward()` 函数，基础框架（FM 查询 API、提示格式化）<100 行；
- 档案机制保留**所有历史发现**，包括低分但结构新颖的 agent（ stepping-stones）。

**实验结果**：

- ARC 逻辑谜题：发现 agent 显著超越 SOTA 手工设计；
- DROP 阅读理解：F1 +13.6；MGSM 数学：准确率 +14.4；
- **跨域迁移**：从数学到阅读理解，迁移后仍 +25.9% GSM8K、+13.2% GSM-Hard。

### 3.2 AgentBreeder：进化 + Meta-Agent

- **论文**：AgentBreeder: Evolving Multi-Agent Scaffolds via Quality-Diversity Search  
  2025 · arXiv:2502.00757

在 ADAS 基础上引入：

- **MAP-Elites 式质量多样性搜索** — 按架构特征聚类，每个 niche 保留 Pareto 前沿；
- **交叉（Crossover）+ 变异（Mutation）** — Meta Agent 对两个精英 agent 做代码交叉；
- **多目标优化** — 同时优化能力（capability）和安全性（safety）。

### 3.3 ReCreate：经验驱动的 Agent 优化

- **论文**：ReCreate: Reasoning and Creating Domain Agents Driven by Experience  
  arXiv:2601.11100, 2026-04

区别于 ADAS 的「粗粒度标量分数」评估，ReCreate 把**完整交互经验**（轨迹、日志、执行产物、验证器输出）输入给 ReCreate-Agent，提出**有针对性的 scaffold 编辑**。这是「经验 grounded」的 agent 优化，而非仅靠分数排序。

### 3.4 与 XMclaw 的对比

| 维度 | ADAS / AgentBreeder / ReCreate | XMclaw |
|------|--------------------------------|--------|
| 组

---

## 2. 自动课程（Automatic Curriculum）

### 2.1 Voyager 的自动课程

**论文**：同上（arXiv:2305.16291）  
**源码**：`voyager/agents/curriculum.py`

**CurriculumAgent.propose_next_task** 的核心逻辑：

```python
def propose_next_task(self, events, chest_observation, max_retries=5):
    # 根据当前状态（inventory、biome、completed_tasks、failed_tasks）
    # 由 GPT-4 提出下一个最大化探索的任务
    #  overarching goal: "discover as many diverse things as possible"
    ...
```

课程设计原则（in-context novelty search）：

- 考虑 agent 当前技能水平（`completed_tasks` 数量决定难度）；
- 考虑世界状态（biome、time、inventory）；
- 失败任务会进入 `failed_tasks`，后续课程会回避或简化；
- 成功任务会解锁更复杂的后续任务（类似游戏科技树）。

**Voyager 主循环中的课程更新**：

```python
while True:
    task, context = self.curriculum_agent.propose_next_task(
        events=self.last_events,
        chest_observation=...,
    )
    messages, reward, done, info = self.rollout(task=task, context=context)
    if info["success"]:
        self.skill_manager.add_new_skill(info)          # 成功→学技能
    self.curriculum_agent.update_exploration_progress(info)  # 更新课程进度
```

### 2.2 EvoCurr：LLM 驱动的自主课程 + 行为代码生成

- **论文**：EvoCurr: Self-evolving Curriculum with Behavior Code Generation for Complex Decision-making  
  arXiv:2508.09586, 2025-08

**架构**：三阶段闭环

1. **Curriculum Designer** — 根据当前能力和上一任务结果，生成渐进复杂度的训练任务；
2. **Behavior Coder** — Planner→Coder→Critic 循环，把课程翻译成可执行决策树代码（python-sc2）；
3. **Environment Evaluation** — 胜率、决策准确率反馈回课程设计师，调整难度。

关键发现：

- 直接生成最终任务的代码成功率低；
- 通过课程渐进（5 海军陆战队员→10 海军陆战队员+医疗运输机→…→完整部队），最终任务胜率可达 **100%**；
- 课程会根据胜率阈值自适应：超过阈值则增加复杂度，未达标则回退简化。

### 2.3 SEAgent：自主技能发现 + 课程生成

- **论文**：Agent Skills for Large Language Models: Architecture, Acquisition, Security, and the Path Forward（综述）  
  arXiv:2602.12430, 2026-02
- **SEAgent 出处**：Xu et al., 2026

SEAgent 引入：

- **World State Model** — 逐步轨迹评估；
- **Curriculum Generator** — 从持续更新的软件指南记忆中产生越来越复杂的任务；
- **Specialist-to-Generalist Training** — 把领域专用 agent 的洞察整合到统一模型。

在 OSWorld 的 5 个新软件环境中，成功率从 **11.3% → 34.5%**（+23.2pp）。

### 2.4 与 XMclaw 的对比

| 维度 | Voyager / EvoCurr / SEAgent | XMclaw |
|------|------------------------------|--------|
| 课程驱动者 | **主动探索** — 根据当前能力提议「下一个该学什么」 | **反应式修补** — grader 失败驱动变异，无探索课程 |
| 课程来源 | 环境状态 + 已完成/失败任务历史 | 无；`AutonomyPolicy` 有 `curriculum_edit` 但无自动生成 |
| 难度调节 | 自适应：成功则升难，失败则降难 | 无难度梯度；所有变异在同一技能上随机试探 |
| 目标 | 最大化探索 / 解锁新能力 | 最大化 grader 分数 / 修复失败 |

**差距**：XMclaw 没有「自动课程」层。`AutonomyPolicy`（`xmclaw/cognition/autonomy.py`）把 `curriculum_edit` 标记为 low-risk action，但**没有课程生成器**来主动提议「你还缺哪类能力、去补一个」。当前进化是「哪里漏补哪里」，不是「按能力图谱系统性成长」。

**建议**：

1. 短期：在 `SkillInductor` 成功归纳新技能后，由 LLM 评估「该技能解锁了哪些下游能力」，生成候选课程条目；
2. 中期：维护一个**能力依赖图**（如「git-commit → 需要 commit-message-storyteller → 需要 conventional-commit」），课程按拓扑序提议；
3. 长期：引入 EvoCurr 式的自适应难度——若某类任务连续失败，回退到更简单的子技能组合。

---

## 3. Meta-Agent 组合

### 3.1 ADAS：Automated Design of Agentic Systems

- **论文**：Automated Design of Agentic Systems  
  Hu et al., 2024-08 · arXiv:2408.08435 · **ICLR 2025**  
  [源码](https://github.com/ShengranHu/ADAS) · [社区复现](https://github.com/RayZhhh/adas-ad)

**核心思想**：把 agent 定义在**代码空间**（Python），用一个 meta-agent 迭代地编程新 agent。

**Meta Agent Search 算法**：

```
Archive = 初始 agent 库（如 Chain-of-Thought, Self-Refine）
for t in 1..T:
    context = select(Archive)          # 从档案中选 stepping-stones
    new_agent = meta_agent_program(context)  # GPT-4 写新 agent 的 forward()
    score = evaluate(new_agent)      # 在验证集上跑
    Archive.add(new_agent, score)    # 加入档案，供下一轮使用
```

**关键设计**：

- 搜索空间 = Python 代码（图灵完备，可表达任意 prompt / tool / workflow 组合）；
- Meta-agent 只写 `forward()` 函数，基础框架（FM 查询 API、提示格式化）<100 行；
- 档案机制保留**所有历史发现**，包括低分但结构新颖的 agent（stepping-stones）。

**实验结果**：

- ARC 逻辑谜题：发现 agent 显著超越 SOTA 手工设计；
- DROP 阅读理解：F1 +13.6；MGSM 数学：准确率 +14.4；
- **跨域迁移**：从数学到阅读理解，迁移后仍 +25.9% GSM8K、+13.2% GSM-Hard。

### 3.2 AgentBreeder：进化 + Meta-Agent

- **论文**：AgentBreeder: Evolving Multi-Agent Scaffolds via Quality-Diversity Search  
  2025 · arXiv:2502.00757

在 ADAS 基础上引入：

- **MAP-Elites 式质量多样性搜索** — 按架构特征聚类，每个 niche 保留 Pareto 前沿；
- **交叉（Crossover）+ 变异（Mutation）** — Meta Agent 对两个精英 agent 做代码交叉；
- **多目标优化** — 同时优化能力（capability）和安全性（safety）。

### 3.3 ReCreate：经验驱动的 Agent 优化

- **论文**：ReCreate: Reasoning and Creating Domain Agents Driven by Experience  
  arXiv:2601.11100, 2026-04

区别于 ADAS 的「粗粒度标量分数」评估，ReCreate 把**完整交互经验**（轨迹、日志、执行产物、验证器输出）输入给 ReCreate-Agent，提出**有针对性的 scaffold 编辑**。这是「经验 grounded」的 agent 优化，而非仅靠分数排序。

### 3.4 与 XMclaw 的对比

| 维度 | ADAS / AgentBreeder / ReCreate | XMclaw |
|------|--------------------------------|--------|
| 组合层级 | **Meta-agent 写代码** — 组合 prompt、tool、workflow | **GEPA 式反思变异** — 对已有 HEAD 技能提 1-3 个变体 |
| 搜索空间 | Python 代码（任意拓扑） | 单技能体 + manifest 字段 |
| 档案/ stepping-stones | ✅ 保留所有历史发现 | ❌ 淘汰版本不保留为变异素材（可借鉴） |
| 评估信号 | 标量分数（ADAS）→ 完整经验（ReCreate） | **HonestGrader 多信号**（确定性 + 独立信号）— 领先 |
| 新能力发明 | ✅ Meta-agent 可发明全新模块 | ⚠️ `SkillInductor` 从轨迹归纳，但范围限于已有能力组合 |

**差距**：XMclaw 的 `ReflectiveMutator` + `EvolutionController` 是**单技能级**进化，而 ADAS 是**系统级**进化（meta-agent 重写整个 agent 架构）。XMclaw 在评估层（HonestGrader）领先，但在**组合发明的规模**上落后。

**建议**：

1. 轻量借鉴 ADAS 的 **archive 机制** — 被淘汰的版本不直接丢弃，留作未来变异的 stepping-stone（与 `registry.history()` 的 append-only 日志天然契合）；
2. 中期探索「meta-mutator」— 让 LLM 不只看单个技能，而是看**技能集合 + 任务分布**，提出「新增一个技能来填补能力缺口」或「合并两个技能减少调用链」。
