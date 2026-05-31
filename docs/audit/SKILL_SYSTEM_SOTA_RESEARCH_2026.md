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
| ① 技能表示 | SKILL.md+YAML(Anthropic 开放标准)/ 可执行代码(Voyager)/ MCP(Alita) | `SkillBase` Python 类 + 自定义 manifest | 🔴 未对齐行业标准 |
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
- **XMclaw**:技能 = `SkillBase` ABC(`run(inputs)->SkillResult`)+ pydantic manifest
  (`skill.yaml/json`)+ registry 版本号 + changelog。([skills/AGENTS.md](xmclaw/skills/AGENTS.md))
- **差距**:🔴 表示是**自闭环的 Python 类**,跟刚成行业标准的 SKILL.md **不互通**。
  这意味着 OpenAI/Cursor/Anthropic 生态里成千上万的 SKILL.md 技能 XMclaw 用不了,
  自己的技能也出不去。**建议:加一个 SKILL.md adapter** —— 把 SKILL.md 目录(YAML
  frontmatter + 正文 + 附件脚本)映射成 XMclaw 的 skill 条目(name/description 进
  prefilter+registry,正文进 view,脚本经子进程 runtime 执行)。这是打通生态、ROI
  最高的一步,且不破坏现有进化闭环。

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

## 总结:给决策的优先级

按"打通生态 + 补短板,且不碰护城河"排序:

1. **🔴 ① SKILL.md adapter** — 把 Anthropic 开放标准(已被全行业采纳)的技能目录
   映射进 XMclaw 的 prefilter/registry/runtime。一步同时改善 ①(表示)、⑩(生态)、
   ②(可直接 consume 社区技能)。**最高 ROI**。
2. **🟡 ② 轨迹→技能归纳(Voyager 路线)** — 成功且 HonestGrader 认可的多步轨迹 →
   LLM 抽成带 description 的新 skill 候选 → 进现有 staging+证据门。补"无中生有"。
3. **🟡 ⑩→MCP 客户端接入(Alita 路线)** — 让 agent 能调用/复用外部 MCP 工具,
   缺能力时按需生成。中期。
4. **🟡 ⑧ 复合技能** + **⑦ archive stepping-stones** — 轻量增强,顺带做。
5. **⑨ 自动课程** — 低优先,reactive 进化对个人助理已够。

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
