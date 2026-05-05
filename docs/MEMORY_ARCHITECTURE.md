# XMclaw 学习 / 记忆 / 进化 架构（开发文档）

**Status:** 设计完成，Phase 1 待开发。本文档捕获 2026-05-03 与用户
深度架构盘点的全部产出——从最初的哲学起点（"教他如何学"）一路盘到
具体的数据库 schema 与实施分期。

**Audience:** 任何即将动 `xmclaw/providers/memory/` /
`xmclaw/core/profile/` / `xmclaw/daemon/post_sampling_hooks.py` /
`xmclaw/skills/registry.py` / `xmclaw/daemon/memory_indexer.py` /
`xmclaw/core/persona/` 的开发者。

---

## 0. TL;DR

XMclaw 当前的"自主进化"系统在结构上是**空转的**：实测 8 天运行下来
agent 主动召回类工具调用 0 次、12/12 已注册 skill 调用 0 次、
memory.db 86% items 没 embedding。核心架构债是：**学习的维度被硬
编码、记忆系统不是数据库而是文件堆、进化产物写完没人读**。

本架构提议五个并行转向：

1. **进化 ≡ 记忆**：进化的产物（lessons / preferences / skills /
   journal summaries）就是记忆，按不同 cadence/granularity 写入。
   不存在独立于记忆系统的"进化系统"。
2. **DB 是 truth，markdown 是 view**：mem0 式的结构化数据库存 facts，
   persona markdown 文件是从 DB 渲染出来的视图（用户编辑体验不变）。
3. **记忆按 3 个维度分层**：`kind`（是什么）/ `layer`（多久衰减）/
   `evidence`（多少印证），由数据库 schema 强制。
4. **本能 = 教材每 turn 注入 system prompt 反复磨成反射**：
   `LEARNING.md` 教 agent 如何记、如何想、如何遗忘、何时检索、何时
   固化为 skill；不是写代码模块，是写课程文本。
5. **元学习 = agent 改自己的教材**：周期性 review 自己的记忆产出，
   propose 改 LEARNING.md / extractor prompts → 走与 skill 同样的
   propose→approve 管道，人在回路防 Goodhart 漂移。

---

## 1. 起源：从"教他如何学"到 mem0 式数据库

整段架构盘点是从用户的一个观察开始的。完整脉络保留在此供未来开发
者理解决策：

### 1.1 第一个识别——"学习的维度是被硬编码的"

> *"我们讲进化，让他自主学习，但是学习什么方面是我们规定的，有点
> 狭隘和考虑不到的地方……为什么不教会他如何学习呢？"* — 用户

XMclaw 现状：
- [`proposer.py`](../xmclaw/core/evolution/proposer.py) 写死了哪些
  pattern 算"重复行为"
- `ProfileExtractor` 写死只抽 `preference / style / constraint /
  habit` 四类
- `ExtractMemoriesHook` 写死提取什么不提取什么
- `HonestGrader` 评分维度（功能正确性 / 安全 / 评价者一致性）固定

agent 在我们规定的格子里学得再好，也跨不出格子。这是"narrow"问题，
不是"质量"问题。

### 1.2 三层"什么可以被进化"的尝试性分类

按难度递增：

| 层 | 内容 | 当前状态 |
|----|------|---------|
| L1 自改提示词 | extractor prompts 内容（已 hot-reload，B-182） | 基础设施一半就位，差 propose 路径 |
| L2 自加观察者 | 新增 SkillProposer pattern / ProfileExtractor 维度 | 完全硬编码 |
| L3 自定义"价值" | HonestGrader 评分维度本身 | 完全硬编码 |

核心 trade-off：**开放性 vs Goodhart**——没有外部 ground truth，
agent 容易学出"让自己看起来对"而非"真有用"。所以：
- (1) 必须 human-in-loop（diff approve）
- (2) 评分维度变化要拉真实信号（用户反馈 / 外部测试 / cost）当锚

### 1.3 "本能" vs "自动跑" 的分清

> *"我觉得应该是进化和学习作为他的本能。"* — 用户

关键语义识别：

> **自动跑 ≠ 本能**

今天 `post_sampling_hooks` / proposer / grader 都"自动跑"，但它们
是 **外部观察者**——agent 自己不知道有人在记笔记，也不在思考时调用
这些机制。

**本能的定义**：不可分离。像呼吸一样，思考的同时就在学，学就在思考
里——关掉学习等于关掉思考。这把现有所有进化模块从"功能"降级成"实
现细节"。

### 1.4 候选的"原子本能"草图

按抽象度排（仅作设计参照——本架构最终选择**不固定枚举**而是用教材
来 emerge）：

| 本能 | 触发点 | 每次做什么 |
|------|--------|-----------|
| 预测 | 任何动作前 | 写下"我预期这个调用返回 X 形状"——产生可验证 expectation |
| 惊讶 | 结果回来时 | 实际 vs 预期 diff 大 → 不进下一步，先问 why |
| 类比 | 新任务进入 | "这跟过去哪次像？哪里像、哪里不像" |
| 疑惑标注 | 思考过程中 | 不确定的点显式 mark，不绕过 |
| 凝结 | 一段经历结束 | "这次学到了什么"——不答这个 turn 不算结束 |

每条都不是新功能，是现有机制的 **inversion**：grader 现在事后判分
→ 改成 agent 行动前自己写 prediction（更早抓错，更便宜）；observer
现在外部偷记 → 改成 agent 主动凝结后产出。

### 1.5 "教" — 本架构最终采纳的范式

经过几轮反复后，确定核心动词不是"造"也不是"撤离"，而是"**教**"：

> 小孩学走路也不是被植入一个"行走模块"，是被反复教身体重心怎么调，
> 几十次摔跤后内化成不思考的反射。

落地形式：**LEARNING.md** 进入 persona 文件栈（每 turn 注入 system
prompt），内容是**原则 + 案例 + 当前真实历史的对照**——agent 反复
读 → 反复对照自己的实际行为 → 内化为反射。

这与第 1.4 的"5 条原子本能"并不冲突——本能内容写在教材里 / 由教材
催化，但**不强行枚举固定**。教材会随 agent 自己 review 而进化（第
1.6）。

### 1.6 元学习——agent 改自己的教材

L2 元学习的最终具体形态：

```
agent 周期性 review:
  - 翻自己最近的记忆产出 (memory.db)
  - 对照 LEARNING.md 的原则，找漏洞 / 矛盾
  - propose 一个 markdown diff 给 LEARNING.md（或 extractor_prompts/）
  - 走 skill propose 同样的 propose→approve→materialize 管道
  - user 点头 → 文件改 → 下次 turn 读到新版 → 反射改变
```

L2 不需要新基础设施——复用 evolution pipeline。**propose 的对象从
"新 skill" 扩展到"教材编辑"**。

### 1.7 "进化和记忆是深度绑定的"——最终统一

> *"脑子记不住事情就是个傻子，再怎么进化还是傻子。"* — 用户

这一句把所有线索收敛了：

> **进化 ≡ 记忆。** 进化的产物（lessons / preferences / 新 skill /
> 教材改动）必须真正可写入记忆 + 可读取 + 可使用。否则进化白做。

具体说：

| 我们今天叫它 | 实质上是什么记忆操作 |
|-------------|---------------------|
| `ProfileExtractor` 抽 USER.md | 把观察提炼成结构化用户画像条目 |
| `ExtractMemoriesHook` 写 MEMORY.md | 写持久 lesson |
| `SkillProposer` / `ProposalMaterializer` | 把重复过程晶体化为可寻址记忆 |
| `MutationOrchestrator` 升 skill v2 | 基于使用信号重写已有记忆 |
| Auto-Dream 压缩 MEMORY.md | 记忆 GC / 合并 |
| `HonestGrader` 给分 | 给记忆条目打"有用度"标签 |
| 注入 system prompt | 记忆**读**（hot path） |
| `memory_search` | 记忆按内容召回（cold path） |

**没有独立的"进化栈"。** 上述模块都退到"记忆操作的一种"。本架构
不再区分 `xmclaw/core/evolution/` 和 `xmclaw/providers/memory/`
的语义边界——后续可考虑物理合并。

### 1.8 "记忆系统应该是数据库而不是文件堆"——最终的实施方向

> *"记忆系统应该更偏向于数据库那种，就像 mem0 那样的插件，只不过
> 我们内置了，然后单独配置了向量模型。"* — 用户

把 1.7 落地的具体形态：

- DB 是 truth、markdown 是 view（不破坏用户编辑体验）
- 写入是 mem0 式 upsert（同 fact +1 evidence，矛盾 supersede）
- 检索按 `kind` filter（"只取 lesson"），不只是语义相似
- 向量模型独立配置（已有 qwen3-embedding:0.6b @ 1024D）
- 三轴定位每条记忆（kind / layer / evidence）

这就是第 3 节的具体设计。

---

## 2. 实测审计（2026-05-03，8 天数据）

第 1 节是用户的直觉。第 2 节是数据实锤——证明直觉抓得对。

### 2.1 写→读→用 链每段的状态

| 进化产物 | 写在哪 | 怎么读 | 用上没 |
|---------|-------|--------|-------|
| 经验教训 (MEMORY.md Failure Modes) | post-sampling 自动 | 注入 system prompt 每 turn | ⚠️ attention 暗箱，能看到不等于会用 |
| 历史 (本 session) | sessions.db 自动 | AgentLoop._histories 注入 | ✅ 完整 |
| 历史 (跨 session) | events.db / journal/ | 需 journal_recall tool | ❌ **断**：调用 0 次 / 8 天 |
| 用户画像 | post-sampling auto-extracted → USER.md | 注入 system prompt | ❌ 写过头噪音 + recall_user_preferences 0 调用 |
| Skills | SkillProposer → SKILL.md → 注册成 tool | description 注入 tool schema | ❌ **断**：12 / 12 = 100% dead crystal |
| 工具经验 | post-sampling → TOOLS.md | 注入 system prompt | ⚠️ 同用户画像 |
| `remember` 显式写 | 11 次 | — | ❌ 写完没机制召回 |

### 2.2 召回 surface 调用统计（8 天累计）

```
所有"主动召回"工具调用次数：
  memory_search:            0
  journal_recall:           0
  recall_user_preferences:  0
  memory_pin:               0
  memory_compact:           0
  ─────────────────────────
  Total:                    0
```

memory.db **1113 条向量索引存在 8 天没人查过一次**。

### 2.3 memory.db 结构性错配

```
1136 总 items
  ├─ 160 (14%)  has_embedding=1  ← 实际可向量检索
  └─ 976 (86%)  has_embedding=0  ← 在表里但向量搜不到

kind 分布:
  ├─ kind=turn         976  ← MemoryProvider.sync_turn() 默认实现写的
  └─ kind=file_chunk   160  ← MemoryFileIndexer 写的 ✅ 这部分对的

session 来源分布（污染情况）:
  flow-/probe-/sess-/test* (测试残留): 407 条 = 36%
```

**唯一在工作的是 `MemoryFileIndexer`（160 条 file_chunk 全嵌入）**，
但这只索引了 persona markdown 文件分块——而 markdown 文件本身已经
全量注入 system prompt 了，所以 file_chunk 也基本没被检索（agent
能从 system prompt 直接读到）。

### 2.4 根因（架构层）

[`MemoryProvider.sync_turn()` 的默认实现](../xmclaw/providers/memory/base.py#L95-L118)：

```python
async def sync_turn(self, ...):
    text = f"User: {user_content}\nAssistant: {assistant_content}"
    await self.put("long", MemoryItem(text=text, metadata={"kind": "turn"}))
    # ↑ 没传 embedding；put 看没 embedding 就 has_embedding=0
    # ↑ 也没有 future 流程会回来嵌入它
    # ↑ raw turn 占位永远腐烂在表里
```

每个 turn 都把 user+assistant 整段 dump 进 memory.db、不嵌入、
没 future consumer。这就是 86% 噪音的来源。

它**反映了一个错误假设**：memory.db 应该装"什么都先存着以备未来
extract"。实际正确假设是：**memory.db 应该装已经 extract 好的
facts，turn 流水活在 sessions.db / events.db**。

---

## 3. 架构设计

### 3.1 进化 ≡ 记忆（已在 §1.7 阐述）

不重复，参照第 1.7 节的映射表。

### 3.2 DB 是 truth，markdown 是 view

**反向今天的设定**：

```
今天：markdown 是 truth → memory.db 是 markdown 的（坏）索引
应改：memory.db 是 truth  → markdown 是 DB 的渲染视图
```

**用户编辑体验不破坏**：用户继续在 Web UI 编辑 markdown 文件；
保存时 parser 把 diff 翻译成 DB upsert ops，再重新渲染。markdown
是数据库的 IDE 而不是数据库的源。

mem0 走的就是这条路（DB-only），我们采用其结构思想但保留 markdown
view 以兼容现有 Web UI。

### 3.3 三个轴定位每条记忆

#### Axis 1 — `kind`（这是什么记忆）

| `kind` | 含义 | 写入者 |
|--------|------|-------|
| `identity` | 用户告诉的稳定事实（"我叫张伟"） | 用户 / `update_persona` |
| `principle` | 用户决策 / 显式指令 | 用户 / `update_persona` |
| `preference` | agent 抽出的用户偏好（语言、风格、格式） | `ProfileExtractor` |
| `lesson` | 失败模式 / 经验教训 | `ExtractLessonsHook` |
| `procedure` | skill metadata（描述、参数、何时用） | `ProposalMaterializer` |
| `session_summary` | 跨 session 历史摘要 | 新 hook（B-198） |
| `file_chunk` | persona 文件分块（render + retrieve 用） | `MemoryFileIndexer`（已工作） |
| `curriculum` | LEARNING.md / extractor prompts 自身的版本 | (Phase 5) agent propose 的产物 |

**显式排除**：~~`turn`~~。raw 对话流水活在 `sessions.db` + `events.db`，
不进 memory.db。

#### Axis 2 — `layer`（衰减压力）

| layer | TTL | 用途 |
|-------|-----|------|
| `pinned` | never | identity / 用户 flag "永远记住" |
| `long` | 1 年 | working 满足证据阈值后 promote |
| `working` | 7 天 | 新抽出，等待 promote 或过期 |
| `short` | 1 小时 | scratch / 单 task |

衰减由后台 sweeper 显式执行，**永不**在 query 中悄悄删。

#### Axis 3 — `evidence_count` + `confidence`（多少印证）

每行带：

```
evidence_count:   int       # 这个 fact 被印证过多少次
confidence:       float     # LLM-估值 0-1
last_seen:        float     # 最近一次观察时间戳
superseded_by:    str|None  # 被哪条新行替代
retrieval_count:  int       # 被检索引用过多少次（用于衰减判定）
```

**Promotion**：`working → long` 需 `evidence_count >= 3 AND confidence >= 0.7`

**Demotion**：`long → archived` 需 `now - last_seen > 365d AND retrieval_count == 0`

### 3.4 写入纪律（mem0 式 upsert）

每次抽取器产出候选 fact 时：

```python
def write_fact(kind, content, source):
    candidates = vec_search(kind=kind, text=content, k=3)
    for c in candidates:
        if semantic_match(c, content, threshold=0.85):
            if value_consistent(c, content):
                # 同一 fact 再次出现 → 强化已有行
                c.evidence_count += 1
                c.last_seen = now()
                if c.evidence_count >= 3 and c.confidence >= 0.7:
                    c.layer = "long"
                return
            else:
                # 与已有矛盾 → supersede
                c.superseded_by = new_row.id
                c.archived = True
    # 全新 → insert
    insert(kind=kind, content=content, evidence_count=1, ...)
```

**这就是为什么 USER.md 出现 7 次"中文交流"是错的**——在新 schema
下应是 1 行 `evidence_count=7`。

### 3.5 检索纪律

`memory_search(query, *, kind=None, k=10)` 是首要召回 surface：

- `kind` filter — 只取 `lesson` / 只取 `preference` / etc.
- 语义相似度 — vec match
- 排序 — 默认按 (`evidence_count * confidence`) desc，相同则按 recency

未来 entity 维度（"about user" / "about self" / "about world"）
作为 Phase 4 扩展。

### 3.6 教材层（LEARNING.md）— 本能的来源

记忆操作的伦理写在一个 markdown 文件里、注入 system prompt 每 turn 读：

```
# LEARNING.md — 我如何记、如何想、如何进化

## 思考本身的纪律
- 动手前先写预期；预期错了，那是真信号
- 不确定的点 mark "?? "，不绕过
- 用户说"不是这样"时 fold 进上一步预期，不是道歉转向

## 记忆操作的纪律
- 笔记写给没今天上下文的未来你
- session_id 是元数据不是内容；写 fact 时丢时间戳
- 同一 fact 出现多次：找到旧行 +1 evidence，不要新写一行
- 矛盾的 fact：标 superseded_by，别让两条共存

## 检索的纪律
- 不确定就 memory_search，别凭记忆答
- 用 kind filter 过滤（"我要查 lesson 不查 preference"）
- 找不到就显式说"没找到"，别编

## 写什么、不写什么
- 三次以上 + 步骤稳定 + raw 慢 → 提议 skill_create
- 一次性快照（probe-/flow- session 抽出来的）→ working layer 7 天衰减
- 工具零调用不是删的理由（哥的 B-185）

## 怀疑自己
- "和上面一样"自指要核对
- 找不到证据时说"不知道"，不要编

## 自我修订（这是元学习入口）
- 每 N 次对话 review 自己产出，发现教材有漏的提议改 LEARNING.md
- 提议走 propose pipeline，user approve 才合并
```

**这是 v0 草稿**。Phase 4 落地后 agent 自己 propose 改，是 L2 入口。

### 3.7 元学习入口（L2 自改教材）

agent review 自己的记忆操作历史 → propose 改教材 → 走 propose pipeline →
user approve → 文件改 → 下次 turn 读新版。

操作类型：

| L2 操作 | 改什么 | 例子 |
|---------|-------|------|
| 加原则 | LEARNING.md 加章节/条目 | "加：审计任务的 surprise 灵敏度要调低" |
| 改阈值 | confidence / promote 阈值 | "lesson promote 需 evidence>=3 改 5" |
| 加 kind 维度 | 新增可识别的记忆类型 | "加 risk_aversion kind" |
| 改 extractor prompts | extractor_prompts/*.md | "skill_extractor.md 加 reject primitive wrappers" |
| 删教材内容 | LEARNING.md 删条目 | "类比这条没 ROI 删了" |

**全部走 user approve**——hard 约束防 Goodhart 漂移。

### 3.8 本能 vs 模块的边界

> 本能 = 一次 LLM turn 之内的认知动作。
> 模块 = 跨 turn / sleep-time / 群体级的进化机制。

按这条线分类：

| 现有组件 | 归属 | 理由 |
|---------|------|------|
| persona 文件注入 system prompt | 本能（已是） | turn 开始就读到 |
| `update_persona` / `note_write` | 本能（应该升级为反射写） | turn 末自决要不要写 |
| `ExtractMemoriesHook` / `ProfileExtractor` | 模块（清洗 + 去重层） | post-sampling 跑 |
| `HonestGrader` | 模块（审计角色） | 本能里加 agent 自评，grader 退居外部第二意见 |
| `SkillProposer` + observers | 模块 | 需看跨 turn 数据找 pattern |
| `ProposalMaterializer` | 模块 | 写文件 + 注册 skill 是冷动作 |
| `MutationOrchestrator` | 模块 | 群体进化 = 代际选择 |
| Auto-Dream | 模块 | 类比"睡觉做梦"，cron 跑 |

### 3.9 Anti-requirements

- ❌ **不**把 raw conversation turns 索引进 vec store（活在
  sessions.db / events.db 即可）
- ❌ **不**把整个 MEMORY.md 永远全量注入 system prompt（pinned
  identity 留住，其余按需检索）
- ❌ **不**靠外部 mem0 / hindsight / supermemory 插件作为默认路径。
  builtin 必须开箱即用给到 DB 形态存储；外部插件保持可插拔但可选
- ❌ **不**在 DB 同步时静默删除用户编辑过的 markdown 内容。冲突
  必须显式 surface 给用户
- ❌ **不**枚举固定数量的"原子本能"在代码里 hardcode（§1.4 的 5 条
  只是设计参照，落地是 LEARNING.md 文本，可被 L2 改）
- ❌ **不**让 L2 元学习自动 merge（必须 user approve）

---

## 4. 实施分期

### Phase 1 — 止血（B-197，第一个 PR）

最低代价、最高收益、改动局限可控。

**目标产出**：

- [ ] `MemoryProvider.sync_turn()` 默认实现 → no-op + 日志警告
- [ ] `ProfileExtractor` 双写：保留 USER.md append + 加 DB row
      `kind=preference`，`layer=working`，`evidence_count=1`
- [ ] `ExtractLessonsHook` 双写：保留 MEMORY.md append + 加 DB row
      `kind=lesson`，`layer=working`，`evidence_count=1`
- [ ] `ProposalMaterializer` 双写：保留 SKILL.md + 加 DB row
      `kind=procedure`，`layer=long`
- [ ] `memory_search` 工具加 `kind` 过滤参数
- [ ] 一次性 SQL 清掉现存 976 条 `kind=turn`（带 backup）
- [ ] 一次性 SQL 清掉测试污染（probe-/flow-/sess-/test*/etc.）
- [ ] embedder 失败 retry + ANTI_REQ_VIOLATION 事件 surface

**退出标准**：

- doctor 全绿
- memory.db 中 `kind=turn` rows == 0
- `memory.db` 总 item 数下降到合理（~200-300，主要是 file_chunk +
  迁移过来的精华）
- has_embedding=1 比例 ≥ 95% on rows < 24h old

**预计代价**：~300 LOC + 5-10 个新单测。

### Phase 2 — Upsert 语义

- [ ] DB 加 `evidence_count` / `confidence` / `last_seen` /
      `retrieval_count` / `superseded_by` 列
- [ ] Extractor 写入前先 vec_search 同 kind 近邻；命中合并
- [ ] 后台 promoter task：`working → long` 当阈值满足
- [ ] 后台 demoter task：`long → archived` 长期未引用

**预计代价**：~400 LOC + 10-15 单测。

### Phase 3 — Markdown view 化（B 彻底落地） ✅ 已完成

用户原话："要做就做好不要留债"。下面所有点都已落地，DB 是 truth，
markdown 是从 DB 渲染的 cache。

- [x] **PersonaStore**（[xmclaw/core/persona/store.py](../xmclaw/core/persona/store.py)）—
  ~400 LOC + 16 unit tests。三轴： kind=persona_manual（用户编辑
  prose）+ extracted facts（preference / lesson / procedure /
  curriculum）。AUTO_SECTIONS 路由表决定每个 kind 渲染到哪个文件
  哪个 section。
- [x] **migrate_from_disk** — 首次启动时把现有 markdown 拆成
  manual + bullet 行写 DB。idempotent。daemon 起来 log 显示
  `persona_store.migrated profile=default files=
  {SOUL.md: 0, IDENTITY.md: 0, LEARNING.md: 0, USER.md: 1,
  MEMORY.md: 8, AGENTS.md: 5, TOOLS.md: 3}`
- [x] **render-to-disk on every fact write** — 写 DB 后立刻
  调 `store.render_to_disk()` 刷新 disk 文件。assembler 继续读
  disk（cache）= 读到当前 DB state。
- [x] **post-sampling extractors 改 DB-only**:
  ProfileExtractor → fact_writer 走 store；ExtractMemoriesHook /
  ExtractLessonsHook 在 ctx.persona_store 存在时跳过 markdown
  直写。
- [x] **agent 端 4 个工具走 store**: update_persona /
  remember / memory_pin / learn_about_user 都通过 store 的
  `read_manual` + `set_manual` API 间接走 DB。
- [x] **Web UI 编辑入口走 store**: `PUT /api/v2/profiles/active/
  <file_id>` 调 `store.set_manual(canonical, content)`，自动剥离
  auto section（用户 round-trip 编辑不会破坏）。
- [x] **legacy fallback 全保留**: 没配 persona_store 的 install
  （测试 / 无 vec backend）继续走 markdown 直写——0 BC break。

**实际代价**：~700 LOC + 16 新单测。落地于 commits eab1f2b /
bd6ee8e / 1f37dfd / f1020f3 / 本次 commit。零回归——2030 unit pass。

剩余债：
- 用户 `vim USER.md` 直接 edit 文件后再看，下次 fact 写入会用
  store 渲染覆盖。**不支持**直接 file 编辑——必须走 Web UI 或
  agent tool。可加文件 watcher 但优先级低。
- archived 行（superseded_by 非空）渲染时已跳过，但还没有
  Web UI 触发归档的入口——Phase 4 / Phase 5 添加。

### Phase 4 — 教材 (LEARNING.md) + 自动 retrieval 注入

- [ ] 写 LEARNING.md v0（参考 §3.6 草稿，结合用户已存的 feedback
      memory：unified paths / no-delete-on-zero-usage / 等等）
- [ ] 接进 persona assembler，每 turn 注入 system prompt
- [ ] `AgentLoop` 每 turn 起始：`memory_search(query=user_msg,
      kind=[lesson, preference, principle], k=5)` → 注入临时 system
      message
- [ ] 跑一周测：grader 评分曲线 + 死晶体率 + 重复犯错率

**预计代价**：~200 LOC + LEARNING.md 内容工作（~1 天 collaborative）。

### Phase 5 — 元学习（L2）入口

- [ ] 加 `propose_curriculum_edit` tool — 允许 agent 提议改
      LEARNING.md / extractor_prompts/*
- [ ] propose pipeline 复用 skill propose→approve（hard：人在回路）
- [ ] DB schema 加 `kind=curriculum` 行
- [ ] 跑 1 个月后看 agent 是否真有自我修订行为（不是 spam，不是空 diff）

**预计代价**：~150-300 LOC，取决于 propose pipeline 复用程度。

---

## 5. 跨模块影响 / 兼容性

### 5.1 受影响代码

| 模块 | 改动 |
|------|------|
| [`xmclaw/providers/memory/base.py`](../xmclaw/providers/memory/base.py) | `sync_turn` 默认 no-op |
| [`xmclaw/providers/memory/sqlite_vec.py`](../xmclaw/providers/memory/sqlite_vec.py) | schema 加 evidence/confidence/superseded 列；upsert 路径 |
| [`xmclaw/core/profile/extractor.py`](../xmclaw/core/profile/extractor.py) | sink 双写 markdown + DB row |
| [`xmclaw/daemon/post_sampling_hooks.py`](../xmclaw/daemon/post_sampling_hooks.py) | ExtractLessonsHook 双写 |
| [`xmclaw/daemon/memory_indexer.py`](../xmclaw/daemon/memory_indexer.py) | 保留，可能加 kind 标签精化 |
| [`xmclaw/daemon/proposal_materializer.py`](../xmclaw/daemon/proposal_materializer.py) | skill 注册时同步写 DB row |
| [`xmclaw/providers/tool/builtin.py`](../xmclaw/providers/tool/builtin.py) | `memory_search` 加 kind filter，`memory_pin` 写 layer=pinned |
| [`xmclaw/core/persona/assembler.py`](../xmclaw/core/persona/assembler.py) | （Phase 3）DB → markdown render |
| [`xmclaw/daemon/extractor_prompts.py`](../xmclaw/daemon/extractor_prompts.py) | （Phase 5）写权限给 agent |

### 5.2 数据迁移

memory.db 现有 1136 条数据（86% 是 kind=turn 噪音）：

1. 备份 memory.db → `memory.db.bak.YYYYMMDD`
2. SQL `DELETE WHERE kind=turn`
3. SQL `DELETE WHERE metadata` 包含 `probe-`/`flow-`/`sess-`/`test*`
4. 保留 160 条 `kind=file_chunk`（已 embed）作为 baseline
5. extractor 重新跑一遍历史 chat session 把 fact 抽进 DB（可选——
   只有用户觉得历史值得回收时做）

### 5.3 配置

`daemon/config.example.json` 新增字段（Phase 1）：

```json
{
  "memory": {
    "_comment_b197": "B-197: facts-as-rows storage. Extractors write to DB rows with kind/layer/evidence dimensions, not just markdown.",
    "embedding": { "...": "已存在" },
    "extractor_writes_db": true,
    "promotion_threshold": {
      "evidence_count": 3,
      "confidence": 0.7
    },
    "demotion_threshold": {
      "stale_days": 365,
      "min_retrieval_count": 1
    }
  }
}
```

---

## 6. 待决问题（不阻塞 Phase 1）

1. **Markdown render 在 daemon 启动时全量重写 USER.md 之类，对
   Web UI 用户的"我的编辑还在吗"心理体验**——Phase 3 决策。
   候选：用户编辑过的行加 `editable=true` flag，render 不覆盖。
2. **跨设备 memory 同步**——本架构默认单机；后续要做云同步时
   DB schema 决定 sync 边界（按 kind 同步 / 按 layer 同步）。
3. **embedder 模型变更时怎么办**——qwen3 1024D → 换别的模型必然
   全量 re-embed。schema 加 `embedding_model_version` 列。
4. **隐私 / 选择性遗忘**——用户说"忘掉关于 X 的事"应该 archive
   所有相关行。需要 entity 维度（Phase 4+）才能精确实施。
5. **L2 propose 的"approve UI"**——目前 skill propose 走 SKILL_*
   事件 + Web UI 一个 approve 按钮；curriculum edit 走同样路径还是
   要专门 UI？
6. **本能 token 成本**——LEARNING.md 注入 + 每 turn auto-retrieval
   是否会让 prompt budget 超标？需要 Phase 4 跑通后实测。

---

## 7. 验证计划

### 7.1 静态

- 全部新代码 mypy 干净
- ruff 干净
- import direction 干净
- 单测覆盖每个 kind 的 write/read/upsert 路径

### 7.2 运行时

跑完 Phase 1 后，运行 daemon 24h，验证：

- [ ] memory.db 不再增长 `kind=turn` rows
- [ ] `kind=preference` / `kind=lesson` rows 出现且 evidence_count > 1
      的合并发生
- [ ] embedder 失败率 < 5%
- [ ] 测试污染再不复发

跑完 Phase 4 后：

- [ ] 实测 `memory_search` 调用频次从 0/天 → ≥1/对话
- [ ] grader 评分曲线（agent 是否更少重复犯过的错）
- [ ] Skill 调用率（dead crystal rate 是否下降）

### 7.3 Long-term（3 个月）

- [ ] L2 自改教材机制是否产生有意义提议（不是 spam，不是空 diff）
- [ ] 用户记忆 retrieve 命中率（有相关 fact 时是否能召回到）
- [ ] curriculum 文件是否经过用户多次修改、agent 多次提议——
      这条链是否真的循环起来

---

## 8. 关联资料

- [docs/V2_DEVELOPMENT.md](V2_DEVELOPMENT.md) §1-§3：grader / scheduler /
  controller 的契约（本架构里它们仍存在但语义重定位为"记忆操作"）
- [docs/EVENTS.md](EVENTS.md)：事件流 schema（GRADER_VERDICT /
  SKILL_CANDIDATE_PROPOSED 等不变；新增 MEMORY_OP / FACT_UPSERTED）
- [docs/ARCHITECTURE.md](ARCHITECTURE.md)：本文档作为 §"Memory & Learning"
  节的权威实施细节，ARCHITECTURE.md 主文保持高层视角
- 历史 commit B-41 / B-43（MemoryFileIndexer）/ B-25（MemoryManager）
  / B-26（MemoryProvider 扩展 hooks）/ B-182（extractor prompts hot-reload）：
  当前实现的来源
- 用户记忆中的相关 feedback（已存在 `~/.claude/projects/.../memory/`）：
  - `feedback_unified_paths.md` — 写路径 == 读路径
  - `feedback_no_delete_on_zero_usage.md` — 零调用 ≠ 删
  - `feedback_verify_local_after_pr_merge.md` — 远程合并 ≠ 本地修复
  这些是 LEARNING.md v0 的天然种子内容

---

## 9. 决策日志

| 日期 | 决策 | 理由 |
|------|------|------|
| 2026-05-03 | 进化和记忆合一为单一架构 | "脑子记不住事就是傻子"——分开盘是错的 |
| 2026-05-03 | 不引入 mem0 / hindsight 作为默认 | builtin 应开箱即用 |
| 2026-05-03 | 保留 markdown 作 view 而非废除 | 兼容用户 Web UI 编辑习惯 |
| 2026-05-03 | sync_turn 默认改 no-op 而非删除整个方法 | 子类（hindsight 等）可能 override，保接口 |
| 2026-05-03 | Phase 顺序固定 1→2→3→4→5 不可重排 | upsert 在 view 化之前必须稳；retrieval 在 schema 之后 |
| 2026-05-03 | 用户称呼"哥"／"敬宇"作为 identity kind 永 pin | 已存在 |
| 2026-05-03 | 不固定枚举"5 条原子本能"在代码里 | 落地是 LEARNING.md 文本，可被 L2 改 |
| 2026-05-03 | L2 propose 必须 human approve | 防 Goodhart 漂移 |
| 2026-05-03 | "教"不是"造"也不是"撤离" | 给原则 + 例子 + 自我历史对照，反复读成本能 |
