# Agent 技能系统全栈调研（真实源码 + 论文）

> 2026-05-31。范围:**整个技能系统的每一层**——表示 / 获取 / 披露 / 检索 /
> 生成变异 / 评估 / 晋升 / 组合 / 课程 / 共享 / 安全。对每层答三问:(a) 头部怎么
> 做(论文 + 真实源码出处);(b) XMclaw 现状(读真实代码);(c) 差距 + 建议。
>
> 调研对象(全部有开源代码 + 论文):Voyager、Anthropic Agent Skills、ADAS、
> Alita、DSPy/GEPA,加两篇 2025 self-evolving agents survey。
>
> **一句话核心发现**:XMclaw 的**进化闭环(评估→变异→选择→晋升)其实已是研究级、
> 甚至在"诚实评估"维度领先头部**(HonestGrader 用确定性 ground-truth 检查取代
> LLM-as-judge,大多数系统还在用 LLM 打分);真正落后的是**技能"表示与获取"**——
> 没对齐 2025-12 刚成行业标准的 **SKILL.md**(被 OpenAI/Google/Cursor/Copilot 全部
> 采纳),也缺 Voyager/Alita 那种"从成功轨迹**新造**一个技能/MCP"的归纳能力(XMclaw
> 主要是**改良已有**技能,不太**无中生有**)。

---

## 0. 全栈对照表(先看结论)

| 层 | 头部代表做法 | XMclaw 现状 | 评级 |
|---|---|---|---|
| ① 技能表示 | SKILL.md+YAML(Anthropic 开放标准)/ 可执行代码(Voyager)/ MCP(Alita) | `SkillBase` Python 类 **+ 已支持 SKILL.md**(markdown_skill + user_loader 解析 frontmatter) | 🟢 已消费 SKILL.md(残留:L3 脚本/allowed_tools 强制) |
| ② 获取/作者 | 人写 + LLM 从轨迹**新造**(Voyager/ADAS/Alita) | 主要**改良已有**(ReflectiveMutator) | 🟡 缺新技能归纳 |
| ③ 渐进披露 | 3 级(name/desc → 全文 → 附件)(Anthropic) | 3 步 browse→view→run + prefilter | 🟢 **已对齐** |
| ④ 检索/选择 | desc embedding top-k(Voyager) | token-overlap prefilter + UCB1 bandit | 🟢 甚至更强 |
| ⑤ 生成/变异 | GEPA 反思变异 / DSPy 编译 / Meta-Agent 写代码(ADAS) | ReflectiveMutator(GEPA)+ DSPy SkillMutator | 🟢 **用的就是 SOTA** |
| ⑥ 评估 | 多数 **LLM-as-judge**;少数 env 自验证(Voyager) | **HonestGrader 确定性 ground-truth**(拒绝 LLM-judge) | 🟢 **领先头部** |
| ⑦ 晋升/版本 | 多靠 benchmark 分数;archive(ADAS) | 4 阈值证据门 + Pareto + 不可自晋升 | 🟢 研究级 |
| ⑧ 组合 | skills 调 skills,代码拼接(Voyager) | 基本扁平,无显式组合 | 🟡 缺组合 |
| ⑨ 课程 | 自动课程"下一个学什么"(Voyager) | 由 grader 失败驱动变异,无探索课程 | 🟡 缺主动课程 |
| ⑩ 共享/市场 | SKILL.md 跨厂商生态 / MCP 注册表 | 本地 marketplace_index + user_loader | 🟡 生态隔离 |
| ⑪ 安全/沙箱 | 代码执行沙箱 | 子进程 runtime + 注入扫描 + 约束 | 🟢 够 |

**最该投资(🔴/🟡):① 对齐 SKILL.md(打通生态)、② 从轨迹归纳新技能(Voyager/Alita)、
⑩ MCP 互通。评估/晋升/变异这条进化主轴反而是 XMclaw 的护城河,别动。**

---

## ① 技能表示(技能长什么样)

- **头部**:
  - **Anthropic Agent Skills**(2025-12-18 开放标准,engineering blog):一个技能 =
    一个目录,含 `SKILL.md`;开头 YAML frontmatter 必填 `name` + `description`;可
    bundle 脚本/资源文件,Claude 用 Bash 读取、**按需运行脚本而不把脚本读进上下文**。
    **OpenAI(Codex/ChatGPT)、Google(Gemini CLI)、GitHub Copilot、Cursor 同日全部
    采纳** —— 已是事实标准。
  - **Voyager**(arXiv:2305.16291,`MineDojo/Voyager`):技能 = **可执行 JS 代码**,
    `skills.json` 存 name/code/description。
  - **Alita**(arXiv:2505.20286):技能 = 按需**自动生成的 MCP**。
- **XMclaw**:技能 = `SkillBase` ABC(`run(inputs)->SkillResult`)+ pydantic manifest;
  **同时已原生消费 SKILL.md**——`markdown_skill.py:MarkdownProcedureSkill` + `user_loader`
  扫 `<root>/<id>/SKILL.md`,解析 frontmatter(`name`/`description`/`triggers`/
  `when_to_use`/`allowed_tools`/`paths`/`model`/`created_by`)→ `SkillManifest` →
  `tool_bridge._build_description` → `ToolSpec.description`(因此 prefilter + 新的
  语义索引都吃得到),正文经 `skill_view` 给 agent,运行时返回正文当指令、agent 用
  自带工具执行;还做 B-273 注入扫描 + `versions/v<N>.md` 版本归档。
  ([markdown_skill.py](xmclaw/skills/markdown_skill.py) / [user_loader.py](xmclaw/skills/user_loader.py))
- **差距**:🟢 **复核后纠正**:XMclaw **已经能消费 skills.sh / Claude Code 的 SKILL.md
  生态**(`npx skills add` → `~/.agents/skills/<name>/SKILL.md` 直接被扫描注册),
  之前研究稿把这条误判成 🔴。残留小缺口:(a) Anthropic 的 **L3 bundled 脚本**是"模型
  不读进上下文就确定性运行";XMclaw 是"agent 读正文里的指令、再用 bash 跑脚本"——
  功能等价但非沙箱化确定执行;(b) `allowed_tools` 已解析存储但**运行时尚未强制**
  (注释标 G-05/G-06 待办)。两者都是收尾级,不是阻断级。

## ② 获取 / 作者(技能从哪来)

- **头部**:
  - **Voyager**:`add_new_skill` 在一段程序**成功并通过 self-verification** 后,调
    `generate_skill_description`(LLM 看代码+函数名生成描述),把新技能入库 ——
    **从成功轨迹无中生有**。
  - **ADAS**(arXiv:2408.08435,ICLR'25,`ShengranHu/ADAS`):Meta-Agent **写并迭代
    代码**发明全新 agent 构件,维护 archive 当 stepping-stone。
  - **Alita**:遇到能力缺口就**现场生成 MCP**,GAIA 75% pass@1。
- **XMclaw**:`ReflectiveMutator`(GEPA 式单轮自批判)+ DSPy `SkillMutator` 对**已有
  HEAD 技能**提 1-3 个变体;新技能主要靠 `user_loader` 扫描磁盘 + 人写 demo。
  ([reflective_mutator.py](xmclaw/core/evolution/reflective_mutator.py))
- **差距**:🟡 强在"把已有技能越改越好",弱在"遇到没有的能力**新造一个**"。
  **建议:加一条"轨迹→技能"归纳路**(Voyager 路线)—— 当某次多步任务成功且
  HonestGrader 判定为可复用,让 LLM 把该轨迹抽成一个带 description 的新 skill 候选,
  进 staging,照走现有证据门晋升。与已有 episode/lesson 记忆天然衔接。

## ③ 渐进披露(怎么让 agent 找到该用哪个技能)

- **头部**:Anthropic 3 级 —— L1 只把 `name+description` 进系统提示;L2 命中才读
  全文 SKILL.md;L3 附件按需加载。
- **XMclaw**:**已经是这套** —— `skill_browse → skill_view → skill_run` 三步元工具
  + `prefilter`(token-overlap 把 ~404 技能收敛到 ~12/turn)+ `disclosure_mode`
  inline/unified/auto(超阈值自动切 unified,去掉 per-skill 工具)。
  ([tool_bridge.py / prefilter.py](xmclaw/skills/AGENTS.md))
- **差距**:🟢 **这一层 XMclaw 跟 Anthropic 标准是同一个模式,甚至 prefilter 做得
  更细**。无需改;若做了 ① 的 SKILL.md adapter,description 直接喂这层即可。

## ④ 检索 / 选择

- **头部**:Voyager 用 Chroma 存 **description 的 embedding**,`retrieve_skills` 做
  top-k 相似度召回。
- **XMclaw**:prefilter token-overlap(对 CJK↔英文描述更鲁棒,且 `skill_browse`/
  meta-tool 永远白名单直达)+ **`variant_selector` UCB1 bandit** 在(skill_id,
  version)上按 `GRADER_VERDICT` 在线选版本。([variant_selector.py](xmclaw/skills/AGENTS.md))
- **差距**:🟢 比 Voyager 多了一层**多臂老虎机选最佳版本**,更强。可选增强:在
  prefilter 之外补一路 description-embedding 召回(和记忆 hybrid 一个思路),但非
  急需。

## ⑤ 生成 / 变异(怎么提出更好的技能)

- **头部**:**GEPA**(反思式 prompt 进化)+ **DSPy**(arXiv:2310.03714,编译/优化
  LM 管线,optimizer 自举 demo,比 few-shot 高 25-65%);ADAS 让 meta-agent 直接
  写代码。
- **XMclaw**:`ReflectiveMutator` 实现 GEPA 论文的反思变异步骤(单轮 `(head_source,
  recent_failures)→≤3 候选+自然语言批判`),并**封顶 confidence≤0.6**(无运行时证据
  不准自夸);另有 DSPy 后端 `SkillMutator` 走 `GEPA().compile()`。
- **差距**:🟢 **直接用的就是 SOTA(GEPA+DSPy)**。confidence 封顶体现了诚实原则。
  无需改。

## ⑥ 评估(怎么判断一个技能/版本更好)—— XMclaw 的护城河

- **头部**:**绝大多数自进化系统用 LLM-as-judge**(让大模型给轨迹打分)——
  survey(arXiv:2507.21046 / 2508.07407)明确把"评估可靠性 + 安全"列为开放难题;
  Voyager 用 **env self-verification**(Minecraft 能拿到客观反馈,真实世界拿不到)。
- **XMclaw**:**HonestGrader 用确定性 ground-truth 检查取代 LLM-judge(显式
  anti-requirement #4)** —— `check_ran`(必须有非空、非"fake-success"哨兵输出)/
  `check_returned` / `check_type_matched`(没声明类型就返回 None 不给分)/
  `check_side_effect_observable`(fs/bus/memory 副作用可验证)。每个 True 必须带
  非空 evidence,**绝不 pass-on-trust**;holdout registry 防过拟合。
  ([grader/checks.py](xmclaw/core/grader/checks.py))
- **差距**:🟢 **这是 XMclaw 领先头部的地方**。LLM-judge 会被"自信的胡说"骗;XMclaw
  用可重放的客观信号。**别动这条,它是差异化核心**(也和你一贯的"真做完才报"哲学
  一致)。

## ⑦ 晋升 / 版本(怎么接受一个新技能)

- **头部**:多数直接按 benchmark 分数 promote;ADAS 维护 archive 保留 stepping-stone。
- **XMclaw**:`EvolutionController` 保守四阈值门 —— `plays≥min_plays` /
  `mean≥min_mean` / `gap_over_head≥…` / `best_minus_second≥…`(防噪声);Pareto
  frontier + promotion_policy + constraints + staging GateBundle;**结构性禁止自晋升**
  (controller 只返回决策+evidence,registry 门口强制要 evidence)。
  ([controller.py](xmclaw/core/evolution/controller.py))
- **差距**:🟢 研究级、且比头部更克制。可借鉴 ADAS 的 **archive**:被淘汰的版本不
  直接丢,留作未来变异的 stepping-stone(和你"不要因零调用就删"的原则一致)。轻量增强。

## ⑧ 组合(技能调技能)

- **头部**:Voyager 把召回的技能**代码拼进 `programs`**,新任务可直接调已学技能 ——
  累积式组合学习。
- **XMclaw**:技能基本扁平,`SkillResult` 之间无显式"A 技能内部调用 B 技能"的组合
  原语(组合发生在 LLM 编排层,不在技能层固化)。
- **差距**:🟡 缺"把常被一起调用的技能序列固化成一个复合技能"。可作为 ② 轨迹归纳的
  特例:高频技能链 → 合成复合 skill。中期。

## ⑨ 课程(下一个学什么)

- **头部**:Voyager **自动课程**最大化探索,据当前能力提议下一个该学的技能。
- **XMclaw**:进化由 **grader 失败驱动**(哪条技能最近失败就变异哪条)——是"修补
  反应式",不是"探索式课程"。
- **差距**:🟡 没有主动"我还缺哪类能力、去补一个"的课程。低优先(reactive 对一个
  个人助理够用),但若要走向 Voyager 式开放成长,这是缺口。

## ⑩ 共享 / 市场

- **头部**:SKILL.md 成开放标准后,技能在 Anthropic/OpenAI/Google/Cursor 间**可移植**;
  MCP 有注册表生态。
- **XMclaw**:本地 `docs/skill_marketplace_index.json` 兜底 + `user_loader` 扫
  `~/.xmclaw/skills_user/` + `~/.agents/skills/`。
- **差距**:🟡 生态隔离。做了 ① 的 SKILL.md adapter 就**顺带解决**——能直接 consume
  社区 SKILL.md 包。MCP 客户端接入(Alita 思路)可作为后续。

## ⑪ 安全 / 沙箱

- **头部**:代码执行需沙箱;survey 把安全列为自进化的核心未决问题。
- **XMclaw**:技能跑在子进程 runtime(`providers/runtime/process.py`,globals 不
  跨序列化)+ prompt 注入扫描 + evolution constraints。
- **差距**:🟢 够。若引入外部 SKILL.md / MCP(① ⑩),需把沙箱策略延伸到第三方代码
  (能力白名单、网络/文件围栏)——这是引入生态时的配套前提。

---

## ⑫ 自主调用（autonomous invocation）—— 用户点名为「最重要」，单独深挖

> 问题:怎么让 agent **自己决定**何时、调用哪个技能(不靠用户点名)。这不是单一
> 开关,而是一条 5 段流水线;**任何一段断了,自主调用就垮**。XMclaw 的瓶颈精确地
> 卡在第 1 段(发现)。

### 流水线 5 段 + 头部做法 + XMclaw 现状

| 段 | 作用 | 头部做法(论文/源码) | XMclaw 现状 | 评级 |
|---|---|---|---|---|
| 1. **发现/surfacing** | agent 这一轮**能看到**哪些技能 | **RAG-of-tools / 语义路由**:把每个工具的 desc 嵌入向量,按 query 召回(RAG-MCP:741 工具 token 降 99%、准确率 3.2×;>100 工具不做语义选择就「不可用」) | **token-overlap prefilter**(404→~12),其自述「**CJK query 对英文 desc 命中归零**」 | 🔴 **瓶颈** |
| 2. **是否该调** | 决定用技能还是直接答 | **model-invoked**(Anthropic Skills:只把 name+desc 进系统提示,模型据 desc 自己决定;简单任务不触发,复杂/专门任务才触发)。ReAct(Thought→Action)、Toolformer(把「何时调」训进权重) | 同样 model-invoked:`skill_browse` desc 提示「没看到专门技能就先 browse,别急着 bash/web」+ per-skill 工具进列表 | 🟢 模式对,但**受限于第 1 段召回** |
| 3. **多选一** | 一堆相似技能里选对的 | ToolLLM:神经 API retriever + DFS 决策树(16K API);语义路由器 | prefilter top-k + **UCB1 选最佳版本** | 🟢/🟡 |
| 4. **无用户触发** | 没人发话也能自己用 | 自主 agent 循环 / 主动触发 | CognitiveDaemon + HTN planner + `goal-from-percept-*` 自主会话(turn 内有技能工具访问) | 🟢 基础设施在 |
| 5. **别滥调** | 控制过度/乱调用 | OTC / ToolRL:奖励塑形惩罚过度调用;>10 相似工具→幻觉+成本爆炸 | prefilter 收窄 + `unified` 披露模式(超阈值砍 per-skill 工具,只留 `skill_run`) | 🟢 |

### 关键诊断:瓶颈在第 1 段，且对你(中文用户)尤其致命

XMclaw 的 prefilter 是 **token 重叠**匹配。它的 docstring 自己写明:**「DROPS to zero
on CJK queries against English skill descriptions」**。也就是说——你用中文说需求、技能
描述是英文时,相关的 `skill_<id>` 工具**这一轮根本不会出现在 agent 的工具列表里**。
于是 agent「看不见」那个技能,自然无法自主调用,只能退回 bash / web_search / 直接
回答。第 2 段的 model-invoked 决策再聪明也没用——**它决策的前提是先看得见**。

`skill_browse` 是兜底:即使 prefilter 漏了,agent 仍可主动 browse。但这要求 agent
**每次都想起来**去 browse——而 LLM 经常直接用通用工具糊弄过去。所以现状是「能自主调用,
但召回不稳、对中文掉得厉害」。

### 头部的答案:把发现层换成「语义召回」（RAG-of-tools）

ReAct/Toolformer/Anthropic 都假设**该看到的工具已经在上下文里**——真正的工程难点是
**大规模工具下的「发现」**,2025 的共识答案是 **embedding 语义召回**(RAG-MCP、语义
路由器):给每个技能的 description 算向量,按用户 query 向量召回 top-k。好处正中要害:
- **语言无关** → 彻底解决「中文 query 漏掉英文技能」(向量不看字面 token);
- 准确率 3.2×、token 降 99%(RAG-MCP benchmark);
- **XMclaw 已经有现成的 `EmbeddingService`(记忆系统在用)**,把它接到 prefilter 几乎
  零新依赖。

这是「让他自主调用」**ROI 最高、最直接**的一步:agent 看得见对的技能,model-invoked
决策(第 2 段,本就对齐 Anthropic)立刻生效。配套再把 `skill_browse` 的 desc 写得更
硬一点(「**每个非平凡任务开始前都先 browse 一次**」),双保险。

### 自主调用的建议(按 ROI)

1. **🔴 发现层换/加 embedding 语义召回**(RAG-of-tools)—— 复用 `EmbeddingService`,
   给技能 desc 建向量索引,按 query 召回;token-overlap 留作兜底第二路(hybrid,
   跟记忆系统 BM25+向量同思路)。**直接解决中文漏召回 = 自主调用不稳的根因。**
2. **🟡 强化 model-invoked 提示** —— 系统提示/`skill_browse` desc 明确「非平凡任务
   先 browse」,把「该不该用技能」的触发律写清楚(Anthropic 的经验:desc 是唯一信号)。
3. **🟡 无用户自主触发接技能** —— 让 CognitiveDaemon/proactive 的 goal→plan 在执行步
   里显式走技能召回(而非只用通用工具),把「自主使用技能」从 chat 扩到后台。
4. **🟢 保留 UCB1 + unified 披露 + OTC 式克制** —— 已对齐 SOTA,别动。

---

## 总结:给决策的优先级

按"先让自主调用真的稳(用户点名的最重要),再打通生态,且不碰护城河"排序:

1. **✅ ⑫ 自主调用:发现层 embedding 语义召回(RAG-of-tools)— 已落地**
   (commit 0492340)。`SkillSemanticIndex` 复用 `EmbeddingService` 给技能 desc 建向量
   索引,prefilter `token + 3.0×cosine` 融合,中文 query 零 token 重叠也能凭语义过门;
   后台 warm 不占热路径。**直接根治「中文场景 agent 不会自己用技能」**。
2. **✅ ① SKILL.md** — **复核发现已实现**(markdown_skill + user_loader 解析 frontmatter,
   description 已流进语义索引)。残留收尾:L3 脚本确定执行 + allowed_tools 运行时强制。
3. **✅ ② 轨迹→技能归纳(Voyager 路线)— 已落地**(`xmclaw/skills/inductor.py`):成功
   多步轨迹 → `SkillInductor` LLM 合成新 skill 候选(守卫+去重+skip)→ 写**未信任**
   `.proposed` SKILL.md(永不自动晋升,走证据门)。后台 loop opt-in
   (`skills.induction.enabled`,默认 OFF)。补齐"无中生有"。
4. **🟡 ⑩→MCP 客户端接入(Alita 路线)** — 调用/复用外部 MCP 工具,缺能力按需生成。
5. **🟡 ⑧ 复合技能** + **⑦ archive stepping-stones** — 轻量增强,顺带做。
6. **⑨ 自动课程** — 低优先,reactive 进化对个人助理已够。

> **一句话**:XMclaw 的技能**进化引擎**(诚实评估⑥ + GEPA 变异⑤ + UCB1 选择④ +
> 证据门晋升⑦)是**研究级、且在"诚实评估"上领先头部**,这是护城河,别动。短板全在
> **技能的"表示与流通"**——没接上 2025-12 刚统一的 SKILL.md 标准、缺从轨迹**新造**
> 技能的能力。补这两处(尤其 SKILL.md adapter),XMclaw 就能一边保留独有的诚实进化
> 闭环,一边接入全行业的技能生态。

---

## 出处

- Voyager — arXiv:2305.16291 / `github.com/MineDojo/Voyager`(`voyager/agents/skill.py`:add_new_skill / retrieve_skills,Chroma desc-embedding)
- Anthropic Agent Skills — anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills(SKILL.md + YAML name/description + 3 级渐进披露,2025-12-18 开放标准)
- ADAS(Meta Agent Search)— arXiv:2408.08435(ICLR 2025)/ `github.com/ShengranHu/ADAS`
- Alita — arXiv:2505.20286(GAIA 75.15% pass@1,MCP 自生成)
- DSPy — arXiv:2310.03714(NeurIPS 2023)/ Stanford NLP
- GEPA — 反思式 prompt 进化(XMclaw reflective_mutator 的来源)
- Self-evolving agents survey — arXiv:2507.21046(What/When/How/Where to Evolve)、arXiv:2508.07407(Comprehensive Survey)/ `github.com/EvoAgentX/Awesome-Self-Evolving-Agents`
- 自主调用 / 工具选择:ReAct(arXiv:2210.03629)、Toolformer(arXiv:2302.04761)、ToolLLM/ToolBench(arXiv:2307.16789,ICLR'24)、Gorilla(arXiv:2305.15334)、RAG-MCP / 语义路由(RAG-of-tools,2025-26:741 工具 token −99% / 准确率 3.2×;>100 工具无语义选择即不可用)、OTC/ToolRL(过度调用惩罚)、Anthropic Skills「model-invoked」(desc 是触发唯一信号)
