# XMclaw 技能系统深度调研报告

> **调研日期**：2026-06-06  
> **调研范围**：Agent 技能系统全栈——表示 / 获取 / 披露 / 检索 / 生成变异 / 评估 / 晋升 / 组合 / 课程 / 共享 / 安全 / 自主调用  
> **调研方法**：论文检索 + 真实源码分析 + 与 XMclaw 现状对比  
> **前置阅读**：`docs/audit/SKILL_SYSTEM_SOTA_RESEARCH_2026.md`

---

## 0. 执行摘要

XMclaw 的技能系统经过 2026-05-31 的复核，已有显著进展：**SKILL.md 消费**、**语义自主调用**、**轨迹→技能归纳** 三项此前被误判为缺口的 capability 已确认落地。本调研在复核基础上，对 2025-2026 年最新研究进展进行深度追踪，覆盖 **5 大维度**、**20+ 篇论文**、**10+ 个真实开源实现**。

**一句话核心结论**：XMclaw 的 **进化引擎**（HonestGrader 确定性评估 + GEPA 反思变异 + UCB1 在线选择 + 四阈值证据门晋升）是 **研究级护城河**，在「评估诚实性」维度领先行业头部；真正决定能否接入全行业生态的缺口只有两条：**MCP 注册表对接**（发现 3K+ 外部工具）和 **Alita 式 MCP 自生成**（遇到缺口时自动造工具）。

---

## 1. 全栈对照表（先看结论）

| 层 | 头部代表做法 | XMclaw 现状 | 评级 | 优先级 |
|---|---|---|---|---|
| **① 技能表示** | SKILL.md+YAML（Anthropic 开放标准）/ 可执行代码（Voyager）/ MCP（Alita） | `SkillBase` Python 类 + `MarkdownProcedureSkill` 已消费 SKILL.md | 🟢 | — |
| **② 获取/作者** | 人写 + LLM 从轨迹新造（Voyager/ADAS/Alita） | `SkillInductor` 已落地轨迹→SKILL.md 归纳 | 🟡 | 中 |
| **③ 渐进披露** | 3 级（name/desc → 全文 → 附件）（Anthropic） | `skill_browse → skill_view → skill_run` + `prefilter` + `disclosure_mode` | 🟢 | — |
| **④ 检索/选择** | desc embedding top-k（Voyager）/ RAG-of-tools（RAG-MCP） | `token-overlap` + `SkillSemanticIndex`（cosine 融合）+ UCB1 bandit | 🟢 | — |
| **⑤ 生成/变异** | GEPA 反思变异 / DSPy 编译 / Meta-Agent 写代码（ADAS） | `ReflectiveMutator`（GEPA）+ DSPy `SkillMutator` | 🟢 | — |
| **⑥ 评估** | 多数 LLM-as-judge；少数 env 自验证（Voyager） | **HonestGrader 确定性 ground-truth** + 多信号 Iron Rule | 🟢 | **护城河** |
| **⑦ 晋升/版本** | 多靠 benchmark 分数；archive（ADAS） | 四阈值门 + Pareto + 不可自晋升 | 🟢 | — |
| **⑧ 组合** | skills 调 skills，代码拼接（Voyager） | 基本扁平，无显式组合 | 🟡 | 中 |
| **⑨ 课程** | 自动课程「下一个学什么」（Voyager/EvoCurr） | grader 失败驱动变异，无探索课程 | 🟡 | 低 |
| **⑩ 共享/市场** | SKILL.md 跨厂商生态 / MCP 注册表 | 本地 marketplace + MCPHub 客户端 | 🟡 | **高** |
| **⑪ 安全/沙箱** | 代码执行沙箱 / SkillGuard 运行时拦截 | 子进程 runtime + 注入扫描 + 约束 | 🟢 | — |
| **⑫ 自主调用** | RAG-of-tools / 语义路由 / model-invoked | `SkillSemanticIndex` + `prefilter` + UCB1 | 🟢 | — |

**最该投资（🔴/🟡）**：
1. **🔴 MCP 注册表对接** — 打通 3,012 个外部 MCP server 生态
2. **🔴 Alita 式 MCP 自生成** — 从「改良已有」迈向「无中生有」
3. **🟡 技能组合** — 高频技能链固化为复合技能
4. **🟡 自动课程** — 从反应式修补到主动探索
5. **🟡 安全运行时强制** — `allowed_tools` 从解析到拦截

---

## 2. 技能表示与获取

### 2.1 头部做法

#### Anthropic Agent Skills（2025-12 开放标准）

- **论文/出处**：Anthropic Engineering Blog, 2025-12-18；综述论文 arXiv:2602.12430v4
- **核心设计**：一个技能 = 一个目录，含 `SKILL.md`；开头 YAML frontmatter 必填 `name` + `description`；可 bundle 脚本/资源文件；Claude 用 Bash 读取、**按需运行脚本而不把脚本读进上下文**
- **跨厂商采纳**：OpenAI（Codex/ChatGPT Desktop）、Google（Gemini CLI）、GitHub Copilot、Cursor 在两个月内全部采纳 —— 已是事实标准
- **真实源码**：`github.com/anthropics/skills`（75,600 stars）

```markdown
---
name: deploy-to-vercel
description: Deploy a Next.js app to Vercel with proper environment checks
allowed-tools: [bash, file_read]
---

# Deploy to Vercel

## When to Use
When the user asks to deploy a project to Vercel.

## Steps
1. Read `package.json` to confirm build script exists.
2. Run `vercel --version` to check CLI.
3. Run `vercel --prod` if the user explicitly asks for production.
```

#### Voyager（arXiv:2305.16291）

- **论文**：Wang et al., NVIDIA / Caltech / UT Austin / Stanford / ASU, 2023
- **源码**：`github.com/MineDojo/Voyager`
- **技能表示**：可执行 JavaScript 代码（`skill/code/*.js`）+ 自然语言描述（`skill/description/*.txt`）+ 元数据索引（`skill/skills.json`）+ Chroma 向量数据库（`skill/vectordb/`）
- **关键代码**（`voyager/agents/skill.py`）：

```python
def add_new_skill(self, info):
    program_name = info["program_name"]
    program_code = info["program_code"]
    skill_description = self.generate_skill_description(program_name, program_code)
    self.skills[program_name] = {
        "code": program_code,
        "description": skill_description,
    }
    self.vectordb.add_texts(
        texts=[skill_description],
        metadatas=[{"name": program_name}],
        ids=[program_name],
    )
```

#### Alita（arXiv:2505.20286）

- **论文**：普林斯顿大学 AI Lab, 2025-05-26
- **核心思想**：**Minimal Predefinition + Maximal Self-Evolution**。遇到能力缺口时：MCP Brainstorming → 开源搜索 → 脚本生成 → 验证执行 → MCP 封装
- **GAIA 成绩**：75.15% pass@1 / 87.27% pass@3

```python
class AlitaManagerAgent:
    def solve(self, task: str) -> str:
        gap = self.identify_capability_gap(task)
        if gap:
            ideas = self.mcp_brainstorm(gap)
            resources = self.web_agent.search(ideas)
            script = self.script_generator.write(resources, gap)
            ok, feedback = self.code_runner.test(script)
            if not ok:
                script = self.self_correct(script, feedback)
            mcp_server = self.package_as_mcp(script, gap)
            self.mcp_box.store(mcp_server)
        return self.execute_with_mcps(task)
```

### 2.2 XMclaw 现状

XMclaw 已原生消费 SKILL.md：

- `MarkdownProcedureSkill`（`xmclaw/skills/markdown_skill.py`）包装 SKILL.md 为可调用 Skill
- `user_loader`（`xmclaw/skills/user_loader.py`）扫描 `~/.agents/skills/<name>/SKILL.md`，解析 frontmatter（`name`/`description`/`triggers`/`when_to_use`/`allowed_tools`/`paths`/`model`/`created_by`）→ `SkillManifest`
- `SkillInductor`（`xmclaw/skills/inductor.py`）从成功轨迹归纳新 SKILL.md，写 `.proposed` 未信任状态

```python
# xmclaw/skills/markdown_skill.py
class MarkdownProcedureSkill(Skill):
    id: str
    body: str
    version: int = 1

    async def run(self, inp: SkillInput) -> SkillOutput:
        body = self.stripped_body
        # B-273: 注入扫描
        decision = apply_policy(body, policy=PolicyMode.DETECT_ONLY, source=SOURCE_SKILL_BODY)
        return SkillOutput(ok=True, result={"instructions": body}, side_effects=[])
```

### 2.3 差距与建议

| 维度 | 头部 | XMclaw | 差距 |
|---|---|---|---|
| 格式消费 | SKILL.md + YAML frontmatter 事实标准 | `MarkdownProcedureSkill` + `user_loader` 完整解析 | 🟢 **已对齐** |
| 渐进披露 | L1 name+desc → L2 全文 → L3 附件/脚本按需 | `skill_browse → skill_view → skill_run` + `prefilter` | 🟢 **模式等价** |
| 跨厂商移植 | 同一 SKILL.md 可在 Claude/Codex/Copilot/Cursor 间运行 | 扫描 `~/.agents/skills/` 直接消费社区包 | 🟢 **已兼容** |
| L3 脚本执行 | Claude Code 按需运行 bundled 脚本，**不读进上下文**（确定性执行） | `MarkdownProcedureSkill.run()` 返回 body 给 agent，agent 用自带工具执行 | 🟡 **残留：无沙箱化脚本执行** |
| 轨迹归纳 | Voyager `add_new_skill`：成功轨迹→可执行代码→自动入库 | `SkillInductor`：轨迹→SKILL.md（自然语言指令） | 🟡 **产出为自然语言，非可执行代码** |
| MCP 自生成 | Alita：缺口分析→代码生成→MCP 封装→复用 | `MCPHub` 可消费外部 MCP，但**不会自生成** | 🔴 **缺 MCP 自生成** |

**建议**：
1. **🟡 L3 脚本沙箱化执行** — 对高频技能，探索生成确定性子程序（Python 函数或 bash 脚本）作为技能体，降低 LLM 重复推理的 token 开销
2. **🔴 Alita 式 MCP 自生成** — 在 `SkillInductor` 基础上增加「代码生成 + MCP 封装」路径：当轨迹归纳无法覆盖能力缺口时，让 LLM 写 Python 脚本 → `MCPBridge` 封装 → 存入 `mcp_box/`

---

## 3. 技能评估与进化

### 3.1 头部做法

#### GEPA（反思式 Prompt 进化）

- **论文**：Agrawal et al., ICLR 2026 (Oral), arXiv:2507.19457
- **源码**：`github.com/allthingssecurity/GEPA`
- **核心主张**：将「反思」与「进化」结合，用自然语言反思计划驱动 prompt 变异，比标量奖励的 RL（GRPO）样本效率高 35 倍
- **三步骤循环**：
  1. **选择 + 评估**：维护 Pareto archive，随机采样非支配候选，在 mini-batch 上评估
  2. **更新 meta-reflection**：`R_t := MetaReflect(H_t; M_critic)`，critic 看代表性高/低表现样本，提炼成功模式和失败模式
  3. **进化 prompt**：对评估的 prompt 做 textual-gradient 进化，仅当 offspring 得分更高时才接受

```python
# GEPA 核心循环（来自官方 repo 概念）
class GEPA:
    def evolve(self, population, tasks):
        for generation in range(max_generations):
            scores = self.evaluator.evaluate(population, tasks)
            front = self.pareto_selector.select(population, scores)
            reflection = self.reflector.reflect(front, scores)
            offspring = self.mutator.mutate(front, reflection)
            population = front + offspring
        return best_candidate
```

#### DSPy（编译/优化 LM 管线）

- **论文**：Khattab et al., NeurIPS 2023 / ICLR 2024, arXiv:2310.03714
- **源码**：`github.com/stanfordnlp/dspy`（~20k stars）
- **核心主张**：把 LM pipeline 抽象为「文本变换图」，用声明式模块替代脆弱 prompt 模板，通过编译器自动优化 prompts 和 weights
- **关键数据**：编译后 GPT-3.5 比标准 few-shot 高 25-65%；llama2-13b-chat 比专家 demonstration 高 16-40%

#### Self-evolving Agents Survey

- **论文**：
  - Gao et al., arXiv:2507.21046（What/When/How/Where 四维度分类）
  - Fang et al., arXiv:2508.07407（Comprehensive Survey）
- **核心发现**：survey 把「评估可靠性 + 安全」列为自进化 agent 的最大瓶颈；评估目标分为 Adaptivity / Retention / Generalization / Safety / Efficiency 五类

#### 2025-2026 最新方法

| 论文 | 方法 | 核心创新 | 与 XMclaw 关系 |
|---|---|---|---|
| **SkillRL** (arXiv:2602.08234) | 递归技能增强 RL | SKILLBANK 层次技能库 + GRPO 联合优化策略和技能库 | 可借鉴：把技能库嵌入 RL 训练 |
| **EvoSkill** (arXiv:2603.02766) | 失败驱动的技能发现 | Proposer 分析失败 → 诊断 → 提出新 skill | 与 `SkillInductor` 思路一致 |
| **ReSkill** (arXiv:2606.01619) | 技能创建与策略优化和解 | GRPO 组内采样 + Thompson Sampling | UCB1 → Thompson Sampling |
| **SkillSmith** (arXiv:2606.01314) | 技能-工具共进化 | 同时进化技能和工具 | 中长期方向 |
| **SAGE** (Dec 2025) | Skill Augmented GRPO | 系统地把技能库纳入 RL 训练 | 同 SkillRL 方向 |
| **FlashEvolve** (arXiv:2605.08520) | 异步进化加速 | worker-queue + artifact-pool，GEPA 吞吐量 3.5× | 性能优化方向 |

### 3.2 XMclaw 现状

XMclaw 的进化闭环：

- **评估**：`HonestGrader`（`xmclaw/core/grader/`）确定性 ground-truth 检查（`check_ran`/`check_returned`/`check_type_matched`/`check_side_effect_observable`）+ 三层独立信号（UserFollowup / HoldoutTest / CrossJudge）
- **变异**：`ReflectiveMutator`（`xmclaw/core/evolution/reflective_mutator.py`）GEPA 式单轮自批判，confidence ≤ 0.6 封顶
- **选择**：`VariantSelector`（`xmclaw/skills/variant_selector.py`）UCB1 bandit 在 (skill_id, version) 上在线选版本
- **晋升**：`EvolutionController`（`xmclaw/core/evolution/controller.py`）四阈值门 + Pareto frontier + 结构性禁止自晋升

```python
# XMclaw 评估架构（Iron Rule #1）
# Signal A — 确定性检查
det_score = weighted(check_ran, check_returned, check_type_matched, check_side_effect)
# Signal B — 独立信号
ind_score = first_fires(UserFollowupSignal, HoldoutTestSignal, CrossJudgeSignal)
# 组合规则
if ind_score is None:
    final = det_score; promote_eligible = False  # Iron Rule #1
else:
    final = 0.6 * det_score + 0.4 * ind_score
    promote_eligible = (det >= 0.6 AND ind >= 0.5)
```

### 3.3 差距与建议

| 维度 | 头部 | XMclaw | 差距 |
|---|---|---|---|
| 反思深度 | GEPA 全局 meta-reflection `R_t` 跨迭代累积 | 单轮 `(head_source, recent_failures) → ≤3 候选`，无跨轮 meta-reflection 记忆 | 🟡 **缺 meta-reflection 记忆** |
| Pareto 选择 | GEPA 实例级 Pareto archive | `ParetoFrontier` per-context 分桶 | 🟢 **已对齐甚至更强** |
| 样本效率 | GEPA 35× fewer rollouts than GRPO | 未做系统级 benchmark | 🟡 **缺 benchmark** |
| confidence 封顶 | 无显式封顶 | **confidence ≤ 0.6**（Iron Rule） | 🟢 **更强** |
| 评估信号 | 多数 LLM-as-judge | **确定性 ground-truth + 多信号** | 🟢 **领先** |
| RL-in-the-loop | SkillRL/ReSkill/SAGE 在线进化 | 进化与运行时分离 | 🟡 **中长期方向** |
| Thompson Sampling | ReSkill 用 Thompson Sampling | UCB1 | 🟡 **可轻量升级** |

**建议**：
1. **🟡 补全 CrossJudgeSignal plumbing** — 当系统配置多模型时，把第二 judge 的分数写入 event payload
2. **🟡 ReflectiveMutator 增加 meta-reflection 记忆** — 每轮 reflection_summary 持久化，后续轮次携带最近 N 条
3. **🟡 UCB1 → Thompson Sampling** — 已有 plays + mean，只需加 Gaussian 采样
4. **🟡 失败诊断结构化** — 在 `_build_prompt` 前加 `FailureDiagnoser`，把原始失败提炼成结构化诊断标签
5. **🟢 保持 HonestGrader 不动** — 这是护城河

---

## 4. 技能组合与课程

### 4.1 头部做法

#### Voyager 技能组合

- **论文/出处**：arXiv:2305.16291, `github.com/MineDojo/Voyager`
- **组合方式**：代码级累积 —— 新任务生成代码时，Action Agent 的 system prompt 里已包含所有相关技能的完整源码；GPT-4 可在新生成的 `program_code` 里直接调用已有技能函数
- **本质**：复杂技能由 LLM 在代码生成阶段**自发组合**简单技能而成，非预先定义的组合图

#### 自动课程

**Voyager 自动课程**（`voyager/agents/curriculum.py`）：

```python
def propose_next_task(self, events, chest_observation, max_retries=5):
    # 根据当前状态（inventory、biome、completed_tasks、failed_tasks）
    # 由 GPT-4 提出下一个最大化探索的任务
    ...
```

- 考虑 agent 当前技能水平（`completed_tasks` 数量决定难度）
- 考虑世界状态（biome、time、inventory）
- 失败任务进入 `failed_tasks`，后续课程回避或简化
- 成功任务解锁更复杂的后续任务（类似游戏科技树）

**EvoCurr**（arXiv:2508.09586）：

- 三阶段闭环：Curriculum Designer → Behavior Coder → Environment Evaluation
- 关键发现：通过课程渐进，最终任务胜率可达 **100%**
- 课程根据胜率阈值自适应：超过阈值则增加复杂度，未达标则回退简化

**SEAgent**（arXiv:2602.12430）：

- World State Model + Curriculum Generator + Specialist-to-Generalist Training
- 在 OSWorld 的 5 个新软件环境中，成功率从 **11.3% → 34.5%**（+23.2pp）

#### Meta-Agent 组合（ADAS）

- **论文**：Hu et al., ICLR 2025, arXiv:2408.08435
- **源码**：`github.com/ShengranHu/ADAS`
- **核心思想**：把 agent 定义在代码空间（Python），用 meta-agent 迭代编程新 agent
- **搜索空间**：Python 代码（图灵完备）；Meta-agent 只写 `forward()` 函数
- **档案机制**：保留所有历史发现，包括低分但结构新颖的 agent（stepping-stones）
- **实验结果**：ARC 超越 SOTA 手工设计；DROP F1 +13.6；MGSM +14.4；跨域迁移 +25.9% GSM8K

```
Archive = 初始 agent 库
for t in 1..T:
    context = select(Archive)
    new_agent = meta_agent_program(context)
    score = evaluate(new_agent)
    Archive.add(new_agent, score)
```

### 4.2 XMclaw 现状

- 技能基本扁平，`SkillResult` 之间无显式"A 技能内部调用 B 技能"的组合原语
- 组合发生在 LLM 编排层，不在技能层固化
- 进化由 grader 失败驱动（反应式），无主动课程
- `AutonomyPolicy`（`xmclaw/cognition/autonomy.py`）把 `curriculum_edit` 标记为 low-risk action，但**没有课程生成器**

### 4.3 差距与建议

| 维度 | 头部 | XMclaw | 差距 |
|---|---|---|---|
| 组合方式 | Voyager 代码级累积；ADAS 系统级进化 | 基本扁平，LLM 编排层组合 | 🟡 **缺显式组合原语** |
| 课程驱动 | **主动探索** — 根据当前能力提议「下一个该学什么」 | **反应式修补** — grader 失败驱动变异 | 🟡 **缺自动课程** |
| 难度调节 | 自适应：成功则升难，失败则降难 | 无难度梯度 | 🟡 **缺难度梯度** |
| 档案机制 | ADAS 保留所有历史发现作为 stepping-stones | 淘汰版本不保留为变异素材 | 🟡 **可借鉴 archive** |

**建议**：
1. **🟡 复合技能** — 对高频技能链，探索生成确定性子程序作为技能体；维护能力依赖图，按拓扑序提议课程
2. **🟡 借鉴 ADAS archive** — 被淘汰的版本不直接丢弃，留作未来变异的 stepping-stone（与 `registry.history()` 的 append-only 日志天然契合）
3. **🟡 短期课程** — 在 `SkillInductor` 成功归纳新技能后，由 LLM 评估「该技能解锁了哪些下游能力」，生成候选课程条目

---

## 5. 技能共享与市场

### 5.1 头部做法

#### MCP 生态

- **规范**：2025-11-25 版，streamableHttp 成主流；OAuth 2.1 + PKCE 强制；Tool Annotations 新增
- **注册表**：官方 `registry.modelcontextprotocol.io`，**3,012 个唯一 server**
- **安全**：NimbleBrain 审计（2026-03-11），84.6% 有源码，仅 8.5% 使用 OAuth，过去一年 7 个 CVE（含 CVSS 9.6 的 RCE）

```python
# Python 注册表客户端（ben-alkov/mcp-registry-client）
import asyncio
from mcp_registry_client import RegistryClient

async def main():
    async with RegistryClient() as client:
        result = await client.search_servers(name="jira")
        for server in result.servers:
            print(f"{server.name}: {server.description}")

asyncio.run(main())
```

#### skills.sh 生态

- **论文**：Skilldex, arXiv:2604.16911v1
- **规模**：Skills.sh **83K+ skills，8M+ installs**；SkillsMP 400K+ skills（语义搜索爬取）
- **安全**：secure-skills fork 集成 Snyk/Socket/VT 三方审计

#### 2025-2026 新方法

| 论文 | 方法 | 核心创新 |
|---|---|---|
| **SkillGuard** (arXiv:2606.03024) | 运行时权限框架 | 基于 SkillManifest 的运行时权限边界，攻击成功率 32.37% → 23.02% |
| **SkillInject** (arXiv:2602.20156) | 攻击基准 | 前沿模型攻击成功率高达 **80%** |
| **Sealing the Audit–Runtime Gap** (arXiv:2605.05274) | 生命周期三阶段防御 | 提交/锚定/调用阶段防御；**26.1% 市场技能含漏洞** |
| **Microsoft 365 Copilot Agent Store** | 企业级商店 | 生命周期管理、审批、知识源共享控制 |

### 5.2 XMclaw 现状

- **MCP 传输层**：`MCPBridge`（stdio）+ `MCPHttpBridge`（SSE + streamableHttp）已覆盖三种 transport
- **注册表**：本地 `docs/skill_marketplace_index.json` 兜底，**无 MCP 注册表集成**
- **SKILL.md 消费**：`MarkdownProcedureSkill` + `user_loader` 完整解析 frontmatter + body
- **信任模型**：三级信任（UNTRUSTED / INSTALLED / USER）；`allowed_tools` 已解析但**运行时尚未强制**
- **安全**：`security/skill_scanner.py` 扫描源码；`apply_policy` 扫描 SKILL.md body

### 5.3 差距与建议

| 维度 | 头部 | XMclaw | 差距 |
|---|---|---|---|
| MCP Transport | stdio / SSE / streamableHttp + OAuth 2.1 + Tool Annotations | `MCPBridge` + `MCPHttpBridge` 已覆盖；OAuth 未实现；annotations 未消费 | 🟢/🟡 |
| MCP 注册表 | 官方 3K+ server；搜索/版本/发布 API | 本地空 `skill_marketplace_index.json` | 🔴 **缺注册表对接** |
| 技能评分 | Leaderboard、安装量、官方认证、格式符合性评分 | 无 | 🔴 **缺评分机制** |
| 安全验证 | SkillGuard 运行时权限拦截；SkillInject 基准 | `allowed_tools` 解析但未强制；本地源码扫描有 | 🟡 **有声明，无运行时强制** |
| Alita 式自生成 | 缺口分析→代码生成→MCP 封装→复用 | `SkillInductor` 轨迹→SKILL.md；**无代码级工具生成** | 🔴 **缺 MCP 自生成** |

**建议**：
1. **🔴 MCP 注册表对接** — 实现 `MCPRegistryClient`，让 `skill_install` 能从 `registry.modelcontextprotocol.io` 搜索/安装远程 MCP server
2. **🔴 Alita 式 MCP 自生成** — 在 `SkillInductor` 基础上增加「代码生成 + MCP 封装」路径
3. **🟡 安全运行时强制** — 将 `SkillManifest.allowed_tools` 从「解析存储」升级为「运行时拦截」
4. **🟡 技能评分/发现** — 为 `skill_marketplace_index.json` 增加 `install_count`、`grader_score`、`security_audit` 字段

---

## 6. 自主调用与语义检索

### 6.1 头部做法

#### RAG-of-tools / 语义路由

| 工作 | 论文 | 核心数字 | 关键洞察 |
|---|---|---|---|
| **RAG-MCP** | arXiv:2505.03275 | 准确率 43.13% vs 13.62% baseline（3.2×）；token −49% | 把全部 MCP schema 塞进 prompt 会让 LLM「看不见」正确工具；语义检索先召回 top-1 schema 再交给 LLM |
| **Toolshed** | arXiv:2410.14594v2 | 多跳查询检索准确率 95-100%（<500 工具） | Advanced RAG-Tool Fusion：种子技能 → 结构感知扩展 → 依赖补齐 |
| **OATS** | arXiv:2603.13426 | MetaTool NDCG@5 从 0.869 → 0.940 | Outcome-Aware Tool Selection：离线把工具 embedding 向「成功查询质心」插值；零参数、零延迟、零 GPU |
| **Graph-of-Skills** | arXiv:2604.05333v3 | 解决「语义近但功能不足」的 prerequisite gap | 技能是有向图；向量召回后做结构感知扩展 |

**OATS 核心代码**（伪代码，来自论文 §3.2）：

```python
# OATS: 离线 outcome-aware 插值 —— 零服务时开销
def interpolate_embedding(tool_emb, success_query_centroid, alpha=0.15):
    """把工具原始描述 embedding 向「成功查询质心」拉 15%。"""
    return normalize((1 - alpha) * tool_emb + alpha * success_query_centroid)

# 在线检索：纯 CPU、单数位毫秒
retrieved = vector_index.search(query_emb, top_k=K)
```

#### ToolLLM / ToolBench

- **论文**：arXiv:2307.16789, ICLR 2024 Spotlight
- **源码**：`github.com/OpenBMB/ToolBench`
- **规模**：16,464 真实 RESTful APIs，49 类别，12,000+ 任务实例
- **关键组件**：神经 API 检索器（Sentence-BERT 双塔）+ DFSDT（深度优先搜索决策树）+ ToolEval

```python
# DFSDT 推理算法（社区复现版）
def dfsdt_solve(query, apis, max_depth=5):
    stack = [(query, [], 0)]
    best_solution = None
    while stack:
        state, chain, depth = stack.pop()
        if depth > max_depth:
            continue
        candidates = llm.generate_candidates(state, apis)
        for action in candidates:
            new_state = execute_api(action)
            if is_terminal(new_state):
                return chain + [action]
            stack.append((new_state, chain + [action], depth + 1))
    return best_solution
```

#### OTC / ToolRL（过度调用惩罚）

- **OTC-PO**（arXiv:2504.14870）：工具调用减少 68.3%；工具生产力提升 215.4%
- **核心公式**：`R_total = R_correctness + λ · R_tool_efficiency`
- **关键洞察**：现有 RL 方法通常只优化最终答案正确性，导致模型「认知卸载」——能查就查、能算就算；OTC-PO 通过联合奖励，鼓励模型「先自己想想，必要时才调用工具」

### 6.2 XMclaw 现状

XMclaw 自主调用流水线（5 段全通）：

1. **发现/surfacing**：`SkillSemanticIndex`（`xmclaw/skills/semantic_index.py`）复用 `EmbeddingService`，`token + 3.0×cosine` 融合
2. **是否该调**：model-invoked，`skill_browse` desc 提示「没看到专门技能就先 browse」
3. **多选一**：prefilter top-k + UCB1 选最佳版本
4. **无用户触发**：CognitiveDaemon + HTN planner + `goal-from-percept-*` 自主会话
5. **别滥调**：prefilter 收窄 + `unified` 披露模式

```python
# XMclaw 语义索引（xmclaw/skills/semantic_index.py）
class SkillSemanticIndex:
    def __init__(self, embedder: Any) -> None:
        self._embedder = embedder
        self._vecs: dict[str, tuple[str, tuple[float, ...]]] = {}

    async def scores(self, query: str, specs: list[Any], *, floor: float = 0.30):
        qvec = tuple(await self._embedder.embed(query))
        qn = sum(x * x for x in qvec) ** 0.5
        for name, (_desc, vec) in self._vecs.items():
            c = _cosine(qvec, vec, qn)
            if c >= floor:
                out[name] = c
        return out
```

### 6.3 差距与建议

| 维度 | 头部 | XMclaw | 差距 |
|---|---|---|---|
| 召回信号 | RAG-MCP：纯语义向量召回；OATS：outcome-aware 插值 | `SkillSemanticIndex` token + cosine 融合 | 🟡 **已有语义召回，但缺 outcome-aware 重排** |
| 多工具组合 | Toolshed/GoS：结构感知扩展，自动补齐 prerequisite | 扁平列表，无显式依赖图 | 🟡 **缺「技能图」结构** |
| 多步决策 | ToolLLM DFSDT：显式搜索多条工具链 | 单步 `skill_run`，组合在 LLM 编排层 | 🟡 **缺显式多步决策树** |
| 过度调用惩罚 | OTC-PO：联合奖励，显式效率项 | `unified` 披露 + prefilter 收窄 | 🟡 **系统级收窄有，但缺每次调用的效率评估** |
| 生产延迟 | OATS：单数位毫秒 CPU 预算 | `warm()` 后台 fire-and-forget，热路径只做一次 query embed + 内存 cosine 扫描 | 🟢 **延迟已对齐** |

**建议**：
1. **🟡 outcome-aware 重排** — 引入 OATS 的 outcome-aware 插值，利用 `registry.record_usage()` 已有的 success/failure 统计，计算每个技能的「成功查询质心」
2. **🟡 技能依赖图** — 当技能数 >100 时，从平面列表切换到「语义种子 + 路径条件扩展」（借鉴 Toolshed）
3. **🟡 工具生产力指标** — 引入 `tool_productivity` 到 `SkillUsageStats`（正确数 / 调用数），替代纯成功率
4. **🟢 保留 UCB1 + unified 披露** — 已对齐 SOTA

---

## 7. 综合差距矩阵与优先级

### 7.1 全栈差距矩阵

| 层 | 关键缺口 | 投资优先级 | 预计工作量 | 风险 |
|---|---|---|---|---|
| **① 技能表示** | L3 脚本沙箱化执行 | 中 | 2-3 天 | 低 |
| **② 获取/作者** | Alita 式 MCP 自生成 | **高** | 2-3 周 | 中 |
| **⑧ 组合** | 复合技能 + 技能依赖图 | 中 | 1-2 周 | 低 |
| **⑨ 课程** | 自动课程生成器 | 低 | 2-3 周 | 中 |
| **⑩ 共享/市场** | MCP 注册表对接 | **高** | 1-2 周 | 低 |
| **⑩ 共享/市场** | 技能评分/发现 | 中 | 3-5 天 | 低 |
| **⑪ 安全** | `allowed_tools` 运行时强制 | 中 | 3-5 天 | 低 |
| **⑫ 自主调用** | outcome-aware 重排 | 中 | 3-5 天 | 低 |
| **⑥ 评估** | CrossJudgeSignal plumbing | 中 | 2-3 天 | 低 |
| **⑤ 变异** | meta-reflection 记忆 | 中 | 3-5 天 | 低 |
| **⑤ 变异** | UCB1 → Thompson Sampling | 低 | 1-2 天 | 低 |

### 7.2 给决策的优先级

按「先打通生态（高杠杆），再补内部增强，且不碰护城河」排序：

1. **🔴 MCP 注册表对接** — 实现 `MCPRegistryClient`，让 `skill_install` 能从 `registry.modelcontextprotocol.io` 搜索/安装远程 MCP server。直接打通 3K+ 工具生态，ROI 最高。
2. **🔴 Alita 式 MCP 自生成** — 在 `SkillInductor` 基础上增加「代码生成 + MCP 封装」路径。这是 XMclaw 从「改良已有」到「无中生有」的质变。
3. **🟡 outcome-aware 语义召回** — 引入 OATS 插值，利用已有 usage 统计计算「成功查询质心」，直接提升中文场景自主调用稳定性。
4. **🟡 复合技能 + 技能依赖图** — 对高频技能链固化为复合技能；维护能力依赖图，按拓扑序提议课程。
5. **🟡 安全运行时强制** — 将 `SkillManifest.allowed_tools` 从「解析存储」升级为「运行时拦截」。
6. **🟢 保持进化引擎不动** — HonestGrader、EvolutionController、ReflectiveMutator 是护城河，别动。

---

## 8. 出处索引

| 编号 | 论文/项目 | 链接 |
|---|---|---|
| 1 | Voyager (arXiv:2305.16291) | `github.com/MineDojo/Voyager` |
| 2 | Anthropic Agent Skills | `github.com/anthropics/skills` |
| 3 | ADAS (arXiv:2408.08435, ICLR 2025) | `github.com/ShengranHu/ADAS` |
| 4 | Alita (arXiv:2505.20286) | `github.com/CharlesQ9/Alita` |
| 5 | DSPy (arXiv:2310.03714) | `github.com/stanfordnlp/dspy` |
| 6 | GEPA (arXiv:2507.19457, ICLR 2026 Oral) | `github.com/allthingssecurity/GEPA` |
| 7 | Self-evolving Agents Survey (arXiv:2507.21046) | `github.com/CharlesQ9/Self-Evolving-Agents` |
| 8 | Self-evolving AI Agents Survey (arXiv:2508.07407) | — |
| 9 | RAG-MCP (arXiv:2505.03275) | `memoverflow/rag-mcp` |
| 10 | ToolLLM/ToolBench (arXiv:2307.16789) | `github.com/OpenBMB/ToolBench` |
| 11 | ReAct (arXiv:2210.03629) | — |
| 12 | Toolformer (arXiv:2302.04761) | — |
| 13 | OTC-PO (arXiv:2504.14870) | — |
| 14 | OATS (arXiv:2603.13426) | vLLM Semantic Router Team |
| 15 | Toolshed (arXiv:2410.14594v2) | — |
| 16 | Graph-of-Skills (arXiv:2604.05333v3) | — |
| 17 | SkillRL (arXiv:2602.08234) | — |
| 18 | EvoSkill (arXiv:2603.02766) | — |
| 19 | ReSkill (arXiv:2606.01619) | — |
| 20 | SkillSmith (arXiv:2606.01314) | — |
| 21 | SAGE (Dec 2025) | — |
| 22 | FlashEvolve (arXiv:2605.08520) | — |
| 23 | AutoSkill (arXiv:2603.01145) | — |
| 24 | EvoCurr (arXiv:2508.09586) | — |
| 25 | SEAgent (arXiv:2602.12430) | — |
| 26 | AgentBreeder (arXiv:2502.00757) | — |
| 27 | ReCreate (arXiv:2601.11100) | — |
| 28 | MCP Spec 2025-11-25 | `modelcontextprotocol.io/specification/2025-11-25/changelog` |
| 29 | MCP Registry | `github.com/modelcontextprotocol/registry` |
| 30 | SkillGuard (arXiv:2606.03024) | `github.com/LLMSecurity/skillguard` |
| 31 | SkillInject (arXiv:2602.20156) | `skill-inject.com` |
| 32 | Sealing the Audit–Runtime Gap (arXiv:2605.05274) | — |
| 33 | Supply Chain Security (arXiv:2603.00195) | — |
| 34 | Skilldex (arXiv:2604.16911) | — |
| 35 | Meta-Rewarding (Wu et al., 2025) | — |
| 36 | LWE (arXiv:2512.06751) | — |
| 37 | COSE (arXiv:2605.28010) | — |
| 38 | OpenDeepThink (arXiv:2605.15177) | — |
| 39 | MCP Registry Client | `github.com/ben-alkov/mcp-registry-client` |
| 40 | Zoom MCP Registry | `github.com/zoom/mcp-registry` |
| 41 | OpenAI Skills | `github.com/openai/skills` |
| 42 | Vercel Skills | `github.com/vercel-labs/skills` |
| 43 | secure-skills | `github.com/alonw0/secure-skills` |
| 44 | mcp-compressor (Atlassian Labs, 2026) | — |
| 45 | In-Context Tool Learning (arXiv:2503.21460) | — |
| 46 | Contextual Experience Replay (ACL 2025) | — |
| 47 | SimpleMem (arXiv:2601.02553) | — |
| 48 | COVERT (arXiv:2604.09813) | — |
| 49 | Microsoft 365 Copilot Agent Store | Microsoft Ignite 2025 |

---

> **复核总结（2026-06-06）**：XMclaw 的技能系统经过本调研深度复核，**进化引擎（评估→变异→选择→晋升）是研究级护城河**，SKILL.md 消费和 MCP 传输层也已对齐头部。真正决定能否接入全行业生态的缺口只有两条：**MCP 注册表对接**（发现 3K+ 外部工具）和 **Alita 式 MCP 自生成**（遇到缺口时自动造工具）。补这两处，XMclaw 就能在保留独有诚实进化闭环的同时，成为全技能生态的「通用客户端 + 自进化工厂」。
