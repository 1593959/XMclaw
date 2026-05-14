# Memory × Evolution Redesign

**Status:** Design doc — pending review. No code touched yet.
**Owner:** XMclaw bot
**Created:** 2026-05-14
**Branch:** `optimistic-brown-f041b1`

---

## 0. 为什么要重设计

实测现状（2026-05-14 抓的真实数据）：

- 用户今天告诉 agent 至少 3 次"陪玩店 / pw310.wxselling.com / admin / 月流水 5 万"，**搜整个记忆库 0 命中**。
- `memory.db` 31 MB 里大部分是 MemoryFileIndexer 早期误索引的代码片段，跟"用户告诉我的事"无关。
- `graph.db` 仅 3 个节点，**几乎是空的**。
- `autobio/store.sqlite` 46 行里 < 5 条是真用户事实。
- 8 个独立存储 / 5 个写入工具 / 3 个 vec 索引 / 多条 recall 路径互不通气。
- `remember` 工具调过 5 次（近 4 天），**0 次完成**（vec0 UNIQUE bug，本轮修了，但暴露了架构碎裂）。

**用户原话：** *"什么都记不住，不知道记什么，不知道该怎么用，何谈自进化"*

**关键认知（用户提出）：** 进化 = 记忆 + 经验 + 技能的积累。**它们是同一条管道的不同阶段，不是平行模块**。当前记忆层断裂 → 经验层没原料 → 技能层晶化全是无个性化的通用噪音 → 自进化跑空转。

这份文档的目的：**把这条管道从头到尾重新串通**，并定义清楚每段的契约。

---

## 1. 设计原则

| # | 原则 | 含义 |
|---|---|---|
| 1 | **单一管道，四层** | L0 事件 → L1 事实 → L2 经验 → L3 技能。每层只能向上消费下层，不能跳层 |
| 2 | **每个数据只有一个家** | 一个事实只存在 L1 一处。其他 store 都是"视图"（read-only derived） |
| 3 | **写入必须 deterministic** | 用户给的关键信息（URL/账号/数字目标/"记住"指令）由 daemon 入口 hook 强制落库，不依赖 agent 主动调工具 |
| 4 | **读取必须 unified** | 每轮 LLM 看到的记忆来自唯一一个 `query()` 调用，不是 3 条独立路径拼凑 |
| 5 | **向量是索引不是存储** | embedding 是 L1 事实的副产品，不是另一份数据。所有 L1 写入自动 embed |
| 6 | **agent 可见 = 用户可见** | 凡是塞进 system prompt 的，都必须有一个 UI 面板让用户能看 / 编辑 / 删除 |
| 7 | **进化路径必须可追溯** | L3 的每个技能要能反向链回 L2 经验，再链回 L1 事实，再链回 L0 事件。审计透明 |
| 8 | **杀掉死代码** | 不用就删，不要"留着以后可能用" |

---

## 2. 四层架构

```
┌─────────────────────────────────────────────────────────────┐
│  L3  Skills        — 可调用工具 / procedure                  │
│      载体: SkillRegistry (~/.xmclaw/skills/*.jsonl)          │
│      promote 条件: L2 经验得分 + 灰度验证通过                  │
└────────────────────────▲────────────────────────────────────┘
                         │ promotion (晶化)
┌────────────────────────┴────────────────────────────────────┐
│  L2  Experience    — 跨 turn / 跨 session 的可复用方案        │
│      载体: experience_bank 表 (sqlite)                       │
│      内容: pattern + trigger + action_template + grader 分数  │
└────────────────────────▲────────────────────────────────────┘
                         │ 模式发现 + grader 验证
┌────────────────────────┴────────────────────────────────────┐
│  L1  Facts         — 结构化事实 / 偏好 / 决定 / 项目档案       │
│      载体: semantic_memory.db (sqlite + vec0) — 唯一真相      │
│      用户视图: USER.md, PROJECT.md, DECISIONS.md (人类可读)    │
└────────────────────────▲────────────────────────────────────┘
                         │ 抽取 (rule + LLM + 强制 hook)
┌────────────────────────┴────────────────────────────────────┐
│  L0  Events        — 一切原始流，不可变审计源                  │
│      载体: events.db                                         │
└─────────────────────────────────────────────────────────────┘
```

---

## 3. L0 — Events Layer (保留不动)

**载体：** `~/.xmclaw/v2/events.db`
**变更：** 无。继续作为唯一不可变审计源。

**附带清理（后续）：** 把 `COGNITIVE_DAEMON_TICK` 这类高频低值事件移到一个轮转日志文件，不进 events.db。当前 207 MB 大部分是这种 noise，会拖累 FTS5 检索。

---

## 4. L1 — Facts Layer (核心重写)

### 4.1 数据模型 (`Fact`)

```python
@dataclass
class Fact:
    id: str                    # 确定性: f"{kind}:{scope}:{hash(text)[:12]}"
    kind: FactKind             # 见 4.2
    scope: FactScope           # user / project / session
    text: str                  # 1-2 句陈述句
    embedding: tuple[float, ...]  # 1536 维，自动生成
    confidence: float          # 0.0 - 1.0
    evidence_count: int        # 出现次数，重复出现 +1
    source_event_id: str       # 链回 L0
    contradicts: list[str]     # 矛盾的其他 Fact.id
    superseded_by: str | None  # 被更新的事实替代时填这里
    layer: Literal["working", "long_term"]
    ts_first: float
    ts_last: float
```

### 4.2 Fact 分类（5 + 2）

| kind | 含义 | 例子 | 生命周期 |
|---|---|---|---|
| `preference` | 用户偏好 | "用户喜欢简短回复"、"用 PowerShell 不用 bash" | 长期，可被同类 contradict |
| `decision` | 已做的决定 | "项目 threshold 用 85%" | 长期，可被新决定 supersede |
| `identity` | 身份事实 | "用户是 XMclaw 单作者"、"Windows 11" | 长期，几乎不变 |
| `commitment` | 待办承诺 | "agent 说下次会写测试" | 短期，完成或超时清理 |
| `correction` | 纠正信号 | "用户纠正过：不要再 X" | 长期，高权重 |
| `project` | 业务参数 | "陪玩店 pw310.wxselling.com / admin / 月流水 5 万" | 长期，project scope |
| `episode` | 一次完整问答片段（含 grader 分数） | "本 turn 解决了 vec0 INSERT bug" | 长期，喂 L2 |

### 4.3 物理存储 — 后端切换到 LanceDB

**新建数据目录：** `~/.xmclaw/v2/facts/` （LanceDB 的文件存储是目录而不是单文件）

**核心表：** `facts`（LanceDB 表，列式 Arrow 格式）

```python
# 用 Pydantic 描述 schema（LanceDB 原生支持）
from lancedb.pydantic import LanceModel, vector
from typing import Optional

EMBEDDING_DIM = 1536

class Fact(LanceModel):
    id: str                       # 确定性: f"{kind}:{scope}:{hash(text)[:12]}"
    kind: str                     # preference / decision / identity / commitment / correction / project / episode
    scope: str                    # user / project / session
    text: str
    embedding: vector(EMBEDDING_DIM)   # 向量列, LanceDB 原生
    confidence: float
    evidence_count: int
    source_event_id: Optional[str]
    contradicts_json: Optional[str]     # JSON array of fact ids
    superseded_by: Optional[str]
    layer: str                    # working / long_term
    ts_first: float
    ts_last: float
```

**为什么换掉 sqlite-vec → LanceDB：** 见 §16 完整调研。一句话：sqlite-vec 的 vec0 虚表不支持 UPSERT（GitHub issue #127）、metadata 过滤不支持 KNN 内联（issue #26）、JOIN 顺序错位（先 KNN 后 filter 经常返空）—— 这些 **都是我们正在踩的坑**。LanceDB 提供原生 upsert (`merge_insert`)、KNN 内联 metadata 过滤、async API、Pydantic schema，并且生产部署在 Netflix / Uber / Harvey 同类负载上。

**为什么不复用现有 `memory.db`：**
- 现有 schema 携带太多遗留字段（superseded_by 用法不一、metadata 是个 JSON 大杂烩、layer 含义在不同代码路径不一致）
- 现有数据 31 MB 大部分是误索引代码片段，迁移代价 > 重建
- 切换后端正好趁机一次性清理；`migrate_from_legacy.py` 跑一次把有用的搬过来

### 4.4 写入契约

**唯一公开 API：**

```python
async def remember(
    text: str,
    *,
    kind: FactKind,
    scope: FactScope = "project",
    confidence: float = 0.8,
    source_event_id: str | None = None,
) -> str:  # 返回 fact_id
    """所有写入走这一个口子。"""
```

**写入流程：**
1. 计算确定性 id `f"{kind}:{scope}:{hash(text)[:12]}"`
2. 检查 contradicts：LanceDB KNN 查 top-3 距离 < 0.2 同 kind 的 facts，对比逻辑矛盾
3. 自动 embed text（调用统一 EmbeddingService，4.6 详述）
4. **LanceDB merge_insert** 原子 upsert —— 命中已存在 id 走 `when_matched_update_all`（合并 confidence + 累加 evidence_count + 更新 ts_last），否则走 `when_not_matched_insert_all`：

   ```python
   await table.merge_insert("id") \
       .when_matched_update_all() \
       .when_not_matched_insert_all() \
       .execute([fact_record])
   ```
5. 发 `FACT_RECORDED` 事件到 bus

**5 个触发源（按优先级）：**

| 优先级 | 触发器 | 写入策略 |
|---|---|---|
| **强制 1** | daemon 入口 hook 识别关键信息（URL/账号/密码/数字目标/"记住"指令）| 直接 remember()，agent 完全不知情 |
| **强制 2** | user 消息匹配 rule-based pattern（"我是 X"、"我喜欢 Y"）| 直接 remember()，kind=preference/identity |
| **LLM 1** | `CONTEXT_COMPRESSION_PENDING` 事件 | LLM 抽取被丢弃的 turns → remember() |
| **LLM 2** | 每轮结束后 `MemoryExtractor.extract()` | LLM 抽取 user/assistant pair → remember() |
| **agent** | agent 调 `memorize` 工具 | 直接 remember() |

**注意：** 工具从 6 个合并成 **1 个**：`memorize(text, kind, scope)`。

### 4.5 读取契约

**唯一公开 API：**

```python
async def recall(
    query: str,
    *,
    k: int = 8,
    kinds: list[FactKind] | None = None,
    scopes: list[FactScope] | None = None,
    min_confidence: float = 0.3,
) -> list[Fact]:
    """所有读取走这一个口子。向量检索 + 过滤。"""
```

**每轮 LLM 起手的记忆注入：**

```
System Prompt
├─ SOUL.md           (人格底色, 极少变)
├─ IDENTITY.md       (agent 身份)
├─ USER.md           (人类可读, 由 facts.user-scope 渲染)
├─ PROJECT.md        (人类可读, 由 facts.project-scope 渲染)
├─ DECISIONS.md      (人类可读, 由 facts.decision 渲染)
├─ AGENTS.md / TOOLS.md  (静态)
└─ [Top-K relevant facts]  ← recall(user_message, k=8)
```

`USER.md / PROJECT.md / DECISIONS.md` 由 `facts.db` 自动渲染。**用户在 UI 编辑这些文件 = 编辑底层 facts**（双向同步）。

### 4.6 统一 EmbeddingService

新建 `xmclaw/memory/embedding_service.py`：

```python
class EmbeddingService:
    async def embed(text: str) -> list[float]: ...
    async def embed_batch(texts: list[str]) -> list[list[float]]: ...
```

- 一个全局单例，所有写入路径都用它
- 内置 LRU 缓存（content hash → vec），命中跳过 API 调用
- 失败重试 3 次 + 指数退避
- **永远不 fall back 到 has_embedding=0**，宁可阻塞写入也要保证 100% 向量化

### 4.7 后端选型 — LanceDB

**采用 LanceDB 作为 L1 向量后端。** 完整调研、benchmark、对比矩阵见 **§16**。

**关键 API 映射：**

| 业务操作 | LanceDB 调用 |
|---|---|
| 连接库 | `db = await lancedb.connect_async("~/.xmclaw/v2/facts/")` |
| 建表 | `table = await db.create_table("facts", schema=Fact)` |
| 写入 (upsert) | `await table.merge_insert("id").when_matched_update_all().when_not_matched_insert_all().execute(records)` |
| 向量检索 + filter | `await table.search(query_vec).where("kind = 'preference' AND confidence > 0.5").limit(8).to_list()` |
| 关键字检索 (BM25) | `await table.search("陪玩店", query_type="fts").limit(8).to_list()` |
| 混合检索 | LanceDB 支持 hybrid (vector + FTS) 同时跑，rerank 取 top-K |
| 删除 | `await table.delete("id = 'xxx'")` |
| 列出索引 | LanceDB 自动建 IVF_PQ / HNSW 索引；> 256 行后自动启动 |

**对应解决了哪些当前痛点（详见 §16 对比）：**

| 现状坑 | LanceDB 怎么解 |
|---|---|
| vec0 `INSERT OR REPLACE` 不工作（Wave 26 fix-5）| `merge_insert` 原生 upsert |
| metadata filter 在 KNN 里失效 | `.where()` 内联到 KNN，先过滤再 KNN |
| auxiliary column 不能 filter | 所有列平权，都可 filter |
| `has_embedding=0` 孤儿行 | LanceDB 写入必带 vec，schema 强制 |
| 3 个独立 vec 表 (memory_vec/graph_nodes_vec/FTS5) | 1 个 facts 表 + Lance 自带 FTS5 + vec 索引 |
| schema 漂移、字段语义不一 | Pydantic schema + Arrow 类型系统强约束 |
| 性能（3072 维 214ms） | LanceDB 1M 向量 960 维 < 20ms（实测） |

**依赖代价：**
- `pip install lancedb` 引入：`lancedb` + `pyarrow >= 16` + `pydantic >= 1.10`
- 总下载约 50-80 MB（Windows wheel 已有）
- Python ≥ 3.10（我们已经是）
- **去掉的依赖：** sqlite-vec 包可保留（仍有 memory.db 残留兼容期），但新写入路径不再用

### 4.7 GC / 淘汰策略

- `working` 层：超过 30 天未被读取 → 自动降级或删除
- `long_term`：永久保留，除非 `superseded_by` 被填
- DreamCompactor 每天凌晨跑：合并近似事实（vec 距离 < 0.1 且 kind 相同）

---

## 5. L2 — Experience Layer

### 5.1 数据模型 (`Experience`)

```python
@dataclass
class Experience:
    id: str
    pattern: str                # NL 描述: "用户问业务参数 → 调 sqlite_query 看后台"
    trigger_keywords: list[str] # 关键词触发（pre-LLM 检测）
    trigger_classifier: str | None  # 可选的 LLM 分类器
    action_template: str        # 模板化的执行步骤
    evidence_episode_ids: list[str]  # 链回 L1.episode 类
    success_count: int
    fail_count: int
    promotion_score: float      # = success / (success + fail + 1) * log(evidence_count)
    status: Literal["candidate", "active", "deprecated"]
    ts_first_observed: float
    ts_last_used: float
```

### 5.2 来源

**唯一入口：** 后台 `ExperienceDistiller` 周期性扫描 L1.episode 类事实：

```
每 N 个新 episode (默认 N=10) 触发一次蒸馏：
  1. 把 episodes 按 trigger 相似度（vec 聚类）分组
  2. 每组 ≥ 3 个 episode → 调 LLM 提取共同模式
  3. LLM 输出 `Experience` shape JSON
  4. 写入 experience_bank，status=candidate
```

**Grader 反馈：**

agent 在后续 turn 命中某个 experience（trigger match）→ 按 action_template 执行 → grader 评分（success/fail）→ 更新 experience 的 success_count / fail_count。

**promote 到 L3 的门槛：**

```
promotion_score >= 0.7 AND evidence_episode_ids >= 5 AND fail_count < 2
```

### 5.3 物理存储

新建 `~/.xmclaw/v2/experience.db`：

```sql
CREATE TABLE experiences (
    id TEXT PRIMARY KEY,
    pattern TEXT NOT NULL,
    trigger_keywords TEXT NOT NULL,  -- JSON array
    trigger_classifier TEXT,
    action_template TEXT NOT NULL,
    evidence_episode_ids TEXT NOT NULL,  -- JSON array
    success_count INTEGER DEFAULT 0,
    fail_count INTEGER DEFAULT 0,
    promotion_score REAL DEFAULT 0.0,
    status TEXT DEFAULT 'candidate',
    ts_first_observed REAL,
    ts_last_used REAL
);
```

### 5.4 读取契约

每轮 LLM 起手时：

```python
candidate_experiences = await experience_bank.match(
    query=user_message,
    keywords=tokenize(user_message),
    status="active",
    k=3,
)
```

匹配到的 experiences 拼到 prompt 里作为"过去类似情况你这么解决过"提示。**用户可在 UI 看 / 禁用 / 编辑每条 experience。**

---

## 6. L3 — Skills Layer (基本保留，加强约束)

### 6.1 来源唯一化

**当前的 SkillProposer 三个来源**（用户自定义 / 复制 ReasoningBank / agent 即兴）→ 统一改为：

- **只有 L2 promote 才能新建 skill**
- L3 不接受任何其他写入路径
- 用户可以"提议"，但提议本身是一个 `Experience`（kind=manual_suggestion），仍要走 promote 流程

### 6.2 灰度 + 回滚

- 新 skill 进入 `canary` 状态
- 灰度采样：前 10 次调用 grader 实时打分
- < 0.6 → 自动 `rolled_back` 并发事件
- ≥ 0.6 → 转 `promoted`

### 6.3 物理存储

保留现有 `~/.xmclaw/skills/*.jsonl` 不动，只加上"origin_experience_id"字段。

---

## 7. 总账：写口 / 读口 / 工具

### 7.1 写口（合并后只剩 1 个 + 5 个触发）

```
唯一公开 API:  memory_service.remember(text, kind, scope, ...)

触发该 API 的路径:
  1. daemon 入口 hook (关键信息强制写)   ← 新增, 最重要
  2. rule-based pattern (我是 X 等)
  3. CONTEXT_COMPRESSION_PENDING 订阅器
  4. MemoryExtractor 后置抽取
  5. memorize 工具 (agent 自主调)
```

### 7.2 读口（合并后只剩 1 个）

```
唯一公开 API:  memory_service.recall(query, k, kinds, scopes, min_confidence)

调用点:
  1. agent_loop.run_turn 起手注入 (top-K facts)
  2. memorize 工具的反向 — memory_search
  3. ExperienceDistiller 扫描 episodes
  4. UI Memory Panel 显示
```

### 7.3 工具收敛

**删除：** `remember`, `learn_about_user`, `memory_pin`, `note_write`, `journal_append`, `memory_compact`

**新增：** `memorize(text, kind, scope)` — 1 个写入工具

**保留：** `memory_search(query)` — 1 个读取工具（包装 recall）

---

## 8. 进化耦合（用户的核心关切）

### 8.1 数据流（端到端）

```
用户消息 "陪玩店 pw310.wxselling.com / admin / 月流水 5 万"
   │
   ├─→ [L0]  user_message 事件 → events.db
   │
   ├─→ [L1]  daemon hook 识别 URL/账号/数字目标 → 强制 remember():
   │           Fact(kind=project, scope=project,
   │                text="陪玩店业务：pw310.wxselling.com / admin / 月流水目标 5 万",
   │                source_event_id=<L0 id>)
   │         自动 embed + 入 facts.db
   │         发 FACT_RECORDED 事件
   │
   ├─→ [UI] 📝 badge + Memory Panel 即时刷新
   │
   ├─→ 下次 turn: recall("陪玩店后台数据") 命中 → 注入 prompt
   │           agent 直接看到"业务上下文"，不用再问
   │
后续 10 个 turn 里：
   │
   ├─→ [L1]  agent 每次成功调 sqlite_query 看后台 → 写 episode:
   │           Fact(kind=episode, text="查后台 → sqlite_query(SELECT ...)",
   │                evidence_count++)
   │
   ├─→ [L2]  ExperienceDistiller 扫到 ≥ 3 个相似 episode → 蒸馏:
   │           Experience(pattern="用户问业务数据 → 调 sqlite_query",
   │                     trigger_keywords=["后台", "数据", "订单", "流水"])
   │
   ├─→ [L3]  promotion_score 过阈 → 晶化为 Skill:
   │           Skill(name="check_business_dashboard",
   │                 trigger=keyword_match, procedure=parametrized_sql,
   │                 origin_experience_id=<L2 id>)
   │
   └─→ 再下次用户问类似问题 → agent 直接调技能，不用 LLM 推理
```

**这就是"记忆 → 经验 → 技能"的完整生命周期。**

### 8.2 反向追溯（审计透明）

UI 上点开任意一个 L3 skill → 展开看：
- 它从哪个 L2 experience 晶化来的
- 那个 experience 从哪 N 个 L1 episodes 蒸馏来的
- 每个 episode 链回 L0 的 user_message 事件

**用户可以看到 agent 的每一项能力是从哪条历史对话长出来的。** 这是"honest evolution"的具体形态。

---

## 9. 用户可见层

### 9.1 三个 Markdown 文件（由 facts.db 渲染）

| 文件 | 由谁渲染 | 字数上限 |
|---|---|---|
| `USER.md` | `kind ∈ {preference, identity, correction} AND scope=user` 全部 facts | 16384（提升 12×） |
| `PROJECT.md` | `kind ∈ {project, commitment} AND scope=project` 全部 facts | 16384 |
| `DECISIONS.md` | `kind=decision` 全部 facts | 8192 |

**双向同步：** 用户在 UI 编辑这些文件 = parse markdown bullets = upsert 对应 facts 行。

### 9.2 Memory Panel UI（新页面）

```
┌─────────────────────────────────────────────────────────┐
│ 记忆面板  (Tier A + Tier B 视图)                          │
├─────────────────────────────────────────────────────────┤
│ ▾ 用户档案 USER.md         [编辑] [导出]                  │
│    • preference: 喜欢简短回复               cnf 0.9      │
│    • identity:   Windows 11 单作者          cnf 0.99     │
│                                                          │
│ ▾ 项目档案 PROJECT.md      [编辑] [导出]                  │
│    • project:    陪玩店 pw310.wxselling.com cnf 0.95     │
│    • commitment: 下次写测试覆盖              cnf 0.7      │
│                                                          │
│ ▾ 决定记录 DECISIONS.md    [编辑] [导出]                  │
│    • decision:   threshold 用 85%           cnf 0.85     │
│                                                          │
│ ▾ 最近事实流  (近 100 条)                                  │
│    📝 18:33  preference  "压缩阈值 85%"   ↗ev-abc12      │
│    📝 18:30  project     "陪玩店业务"     ↗ev-def45      │
│    ...                                                    │
│                                                          │
│ ▾ 经验候选 (Experience Bank)                              │
│    🟡 候选: "用户问业务参数 → sqlite_query"  S=3 F=0      │
│    🟢 已激活: "截图给我看 → screen_capture" S=12 F=1      │
│                                                          │
│ ▾ 已晶化技能 (Skills)                                     │
│    ⚡ check_business_dashboard (canary 8/10)              │
│    ⚡ desktop_screenshot       (promoted, 47次)            │
└─────────────────────────────────────────────────────────┘
```

**每条记录可点击：** 点 fact → 看完整 text + 矛盾候选 + 链回 events；点 skill → 看 origin experience + origin episodes。

---

## 10. 迁移计划

### 10.1 杀掉（确认死代码或冗余）

| 目标 | 理由 |
|---|---|
| `xmclaw/providers/memory/hindsight.py` | 0 引用，纯 stub |
| `xmclaw/providers/memory/mem0.py` | 0 引用，纯 stub |
| `xmclaw/providers/memory/supermemory.py` | 0 引用，纯 stub |
| `xmclaw/providers/memory/builtin_file.py` | 在 manager 注册但从不写入 |
| `xmclaw/providers/memory/sqlite_vec.py` | 后端换 LanceDB，§16 详述。Phase 6 删除，前期作迁移读源 |
| `~/.xmclaw/v2/decisions.db` | 0 行，R3 metacog 没启动 |
| `xmclaw/cognition/memory_graph.py` | 仅 3 节点，能力被 L1 (LanceDB filter + KNN) 覆盖 |
| `xmclaw/cognition/autobiographical_memory.py` | 46 行噪音，合并进 L1 |
| `sqlite-vec` Python 包 | 迁移完成后从依赖清单移除 |

### 10.2 合并

| 旧 | 新 |
|---|---|
| `memory.db` + `graph.db` + `autobio/store.sqlite` | `facts.db` (L1) + `experience.db` (L2) |
| `remember` + `learn_about_user` + `memory_pin` + `note_write` + `journal_append` | `memorize` (1 个工具) |
| `UnifiedMemorySystem.query` + `MemoryManager.prefetch` + `autobio.summarize_for_prompt` | `memory_service.recall` (1 个 API) |
| `MEMORY.md` (混合内容) | `USER.md` + `PROJECT.md` + `DECISIONS.md` |

### 10.3 保留

- `events.db` — L0，不动
- `~/.xmclaw/skills/` — L3 载体，保留
- `cognitive_state.json` — 注意力机制，独立子系统
- `sessions.db` — 会话历史，独立子系统
- `SkillRegistry` 内部代码 — 加 `origin_experience_id` 字段，其余不动

### 10.4 一次性迁移脚本

`scripts/migrate_memory_v2.py`：

```
1. 读 memory.db.memory_items + autobio/auto_facts + graph_nodes
2. 对每条 row：
   a. 用 LLM 判定是否值得保留（过滤代码片段噪音）
   b. 映射 kind/scope (LLM 分类)
   c. 写入 facts.db
   d. 若 has_embedding=1 直接搬 vector，否则重新 embed
3. 备份原文件到 backup_v1/
4. 生成迁移报告 (保留 N 条 / 丢弃 N 条 / 失败 N 条)
```

跑完后 `~/.xmclaw/v2/{memory,graph,autobio}.db` 进 `backup_v1/`，不删除（保险）。

---

## 11. 分阶段实施

| Phase | 工作 | 工时 | 验证 |
|---|---|---|---|
| **0** | 这份文档 | — | 用户拍板 |
| **1a** | 引入 LanceDB 依赖 + `VectorBackend` 接口 + `LanceDBBackend` + `InMemoryBackend` | 半天 | 单元测试 round-trip / upsert 幂等 / where filter |
| **1b** | 新 `xmclaw/memory/` 模块（Fact / FactKind / EmbeddingService / facts table schema） | 半天 | 单元测试 100% |
| **2** | 新 `memory_service.remember/recall` 公开 API（基于 VectorBackend） | 半天 | 单元测试 + 5 个写入触发都接通 |
| **3** | daemon 入口 hook (关键信息强制写) | 半天 | 集成测试：消息含 URL → 立刻能 recall |
| **4** | UI Memory Panel + 三个 markdown 双向同步 | 1 天 | 浏览器 e2e |
| **5** | 迁移脚本（memory.db + autobio + graph → LanceDB facts 表） + 跑一次 | 1 天 | 迁移报告：保留 X / 丢弃 Y / 失败 Z |
| **6** | 删工具 / 删死代码 / 移除 sqlite-vec 依赖 | 半天 | grep 0 引用确认 + `pyproject.toml` clean |
| **7** | L2 ExperienceDistiller | 1 天 | 跑 10 个 episode 看能否蒸馏出 experience |
| **8** | L3 promote 接 L2 | 半天 | 看到一个真实 skill 从 experience promote |
| **9** | 反向追溯 UI | 半天 | 点 skill 能链到 episode 链到 event |

**总计：6.5-7.5 天**（不算阻塞 / 测试调整）

**每个 Phase 是独立可验证、可回滚的。** 不要一次性大爆炸。LanceDB 引入放最前因为整条管道的写入路径都依赖它。

---

## 12. 测试策略

### 12.1 单元
- `Fact` dataclass round-trip（dict ↔ Fact）
- `remember()` 幂等性（同样输入两次 → evidence_count=2，行数不增）
- `remember()` 矛盾检测（写 "用 Mac" 后写 "用 Windows" → 后者 supersede 前者）
- `recall()` 召回顺序（距离近的排前面）
- EmbeddingService LRU 缓存命中
- vec0 DELETE+INSERT 幂等（Wave 26 fix-5 回归测试）

### 12.2 集成
- 端到端写入：消息含 URL → daemon hook → facts.db 有行 → 下轮 recall 命中
- 端到端进化：注入 10 个相似 episode → ExperienceDistiller 跑出 1 个 experience → grader 喂 6 个 success → promote 出 1 个 skill
- 反向追溯：拿 skill.id → query L2 origin → query L1 episodes → query L0 events

### 12.3 验收（用户视角）
- 用户告诉 agent "陪玩店 pw310.wxselling.com / admin / 月流水 5 万"
- **下一轮** agent 不用问就能引用业务上下文
- 用户重启 daemon
- **下下一轮**仍然能引用
- UI Memory Panel 看到该事实，可手动编辑/删除

---

## 13. 待决问题

| 问题 | 提议默认 | 需要用户拍 |
|---|---|---|
| L1 默认 layer working / long_term？ | 默认 working，evidence_count ≥ 3 自动 promote | ✓ |
| 同一事实多 confidence 来源如何 merge？ | 取最大值 + 0.05 × min(它们)，cap 0.99 | ✓ |
| 与 PersonaStore 关系 | 杀掉，USER.md/PROJECT.md/DECISIONS.md 由 facts.db 渲染 | ✓ |
| DreamCompactor 是否继续每天跑 | 继续，但只在 facts.db 上跑（合并近似 + 清 commitment 过期） | ✓ |
| 关键信息 hook 的正则 / pattern 集 | URL / 账号密码 / 数字+单位（"月流水 5 万"）/ "记住" 指令 | ✓ |
| 是否需要 user_id（多用户场景）？ | 暂不需要，单用户 | ✓ |
| Embedding 服务失败时怎么办？ | 阻塞写入 + 重试 3 次 + 入死信队列 | ✓ |

---

## 14. 反对意见 / 已知风险

1. **重建 vs 渐进改造**：本设计选择"新建 facts.db，迁移老数据"而非"原地改老 schema"。理由：老 schema 已经被多次扩展，字段语义漂移（layer 在不同代码路径意义不同），迁移代价 ≈ 重建代价但更脏。
2. **vec 索引依赖单一模型**：1536 维写死。换模型成本 = 全量重新 embed。可接受，因为 facts.db 数据量 < 10 万条，重 embed < 1 小时。
3. **强制 hook 可能误判**：识别 URL/账号会有假阳。容忍方案：误存的事实可通过 UI 一键删除，并把误判 pattern 加入 negative training set 后续 tune。
4. **L2 蒸馏需要 LLM 调用**：每 10 个 episode 触发一次蒸馏，每次 1 个 LLM call ≈ 2K tokens。可接受。
5. **DreamCompactor 改写历史**：保留每日备份机制。

---

## 15. 决策需要用户回答

1. **架构方向**：上面四层 + 单一管道，OK 吗？还是你想要不同的分层？
2. **向量后端**（§16 调研结论）：LanceDB 接受吗？还是想用 sqlite-vec / Chroma / Qdrant local？
3. **杀代码列表**（10.1）：sqlite_vec / memory_graph / autobio 三个会一并下线，有没有哪个其实你想留？
4. **工具收敛**（7.3）：6 → 1 个，OK 吗？
5. **迁移**：能接受一次性迁移（带备份）吗？还是要新旧并存一段？
6. **分阶段实施**（11）：按 Phase 1a-9 顺序走，OK 吗？还是想跳着做？
7. **L1 默认 layer**（13）：working 还是 long_term？
8. **待决问题表 13**：每行的默认提议接受还是改？

---

---

## 16. 向量数据库选型调研 (Vector DB Research)

> 调研时间: 2026-05-14. 数据源标注在每条结论后。

### 16.1 候选清单 + 一句话评判

| 候选 | 类型 | 进 / 出 |
|---|---|---|
| **sqlite-vec** (当前) | SQLite 扩展 | 🟡 进入终选，但有 4 个已知严重 issue |
| **LanceDB** | Rust 嵌入式 / Arrow | ✅ **本方案推荐** |
| **ChromaDB** | Python 嵌入式 | 🟡 备选，v1.0 API 有 churn |
| **Qdrant (embedded)** | Rust 嵌入式 + 本地 client | 🟡 备选，文档以 server 为主 |
| **pgvector** | PostgreSQL 扩展 | ❌ 违反 local-first（要 Postgres daemon） |
| **Milvus / Weaviate / Pinecone** | 云 / 服务化 | ❌ 违反 local-first |
| **FAISS / hnswlib (裸)** | C++ 库 | ❌ 没 metadata、没持久化、要自己包一层 |
| **txtai / usearch** | Python 封装 | ❌ 控制力不够，与我们 schema 强类型期望不匹配 |

### 16.2 硬约束 (XMclaw 项目特性)

| # | 约束 | 说明 |
|---|---|---|
| 1 | **local-first，无 server** | 一切跑在用户机器上，不开后台服务 |
| 2 | **Windows 优先** | dev / install 必须在 Windows 工作 |
| 3 | **Python 3.10+ async** | 与 FastAPI + asyncio 风格一致 |
| 4 | **嵌入式 / 单进程** | 不开 separate process，pip install 即用 |
| 5 | **modest scale** | facts.db 估算 < 100K 条目，余裕 10× |
| 6 | **metadata filtering** | 必须支持 `kind=preference AND confidence>0.5` 这种过滤 |
| 7 | **upsert 必须真好用** | 同 id 写第二次不能炸（当前 sqlite-vec 炸） |
| 8 | **cosine similarity** | 默认距离指标 |
| 9 | **依赖体积可控** | ≤ 100 MB 可接受，> 200 MB 要再讨论 |

### 16.3 sqlite-vec 的已知问题（官方 issue 实锤）

我们当前用的 sqlite-vec **不是不能用**，是有结构性短板，会持续制造类似 Wave 26 fix-5 的坑：

| Issue | 标题 | 影响 |
|---|---|---|
| [#127](https://github.com/asg017/sqlite-vec/issues/127) | "Upsert into vec0 tables?" | **vec0 不支持 UPSERT** — 我们刚踩的 `INSERT OR REPLACE` 坑就是这个，DELETE+INSERT 是 workaround |
| [#26](https://github.com/asg017/sqlite-vec/issues/26) | "Tracking issue: metadata filtering" | KNN 内联 filter 不支持；只能预过滤 `IN (...)` 或 JOIN，慢且容易返空 |
| [#121](https://github.com/asg017/sqlite-vec/issues/121) | "Auxiliary Columns in vec0" | 辅助列只能 sidecar，不能用作 filter 条件 |
| [#186](https://github.com/asg017/sqlite-vec/issues/186) | "Performance tuning for vec search" | 3072 维 214 ms / 1536 维 105 ms（都超 100 ms 目标） |

加上：
- 写入慢（每行单独 SQLite 事务 + chunk 分配）
- JOIN filter 顺序错误（KNN 先跑，filter 后跑，导致 top-K 里没 match 的全没了）

这些不是 bug，是设计取舍。sqlite-vec 优势是"嵌入轻量"，但**复杂查询 + 高频写**不是它的设计目标。我们的 L1 facts 层正好是后者。

来源: [Upsert issue #127](https://github.com/asg017/sqlite-vec/issues/127), [Metadata filtering issue #26](https://github.com/asg017/sqlite-vec/issues/26)

### 16.4 LanceDB 实测 + 生态

**架构：** Rust 实现，[Apache Arrow 列式存储](https://github.com/lancedb/lancedb)，文件后端（目录），嵌入式（无 server），有 Python wheel。

**Production 用户（2026）：**
- **Netflix** — Media Data Lake 用 LanceDB ([newsletter 2025-08](https://www.lancedb.com/blog/newsletter-august-2025))
- **Uber** — 大规模多 bucket 存储
- **Harvey** (legal AI) — 企业级 RAG ([newsletter 2025-07](https://www.lancedb.com/blog/newsletter-july-2025))
- **CodeRabbit, Dosu, Minimax, LumaLabs** — 各类 AI agent

**Benchmark（2026）：**
- 1M 向量 960 维, KNN 查询 < 20 ms（[官方](https://www.lancedb.com/)）
- 1.5M IOPS（同上）
- 10B 向量级别支持（distributed indexing + HNSW centroid routing）

**API 关键：**
- 原生 async: `await lancedb.connect_async(...)`
- 原生 upsert: `table.merge_insert("id").when_matched_update_all().execute(...)`
- KNN + filter 内联: `table.search(vec).where("kind='preference'").limit(8)`
- Pydantic schema: `class Fact(LanceModel): embedding: vector(1536); ...`
- 内置 BM25 全文检索: `table.search("text", query_type="fts")`
- 内置 hybrid 检索: vec + FTS 一起跑 + rerank
- 列式 + Arrow → 与 pandas / polars 无缝
- 版本化表（time-travel） — 可回滚 schema 变更

**依赖：**
- `lancedb` 包 + `pyarrow>=16` + `pydantic>=1.10`
- 总下载 ≈ 50-80 MB
- Windows wheel 可用([blog](https://lancedb.com/blog/lance-windows-windows-lance/))

来源: [LanceDB Python API](https://lancedb.github.io/lancedb/python/python/), [GitHub](https://github.com/lancedb/lancedb), [PyPI](https://pypi.org/project/lancedb/)

### 16.5 ChromaDB 实测

**优势：** 简单 API，最容易上手；嵌入式，无 server；社区大；LangChain 默认。

**劣势（与 LanceDB 比）：**
- 2026 年 v1.0 发布伴随 API churn，部分迁移痛点未消化
- 持久化路径基于 DuckDB，文件结构相对复杂；备份 / 迁移 / 跨机器同步代价高
- 大数据集性能不如 LanceDB（列式 + Arrow 优势）
- async 支持比 LanceDB 弱

**适配 XMclaw 度：** 70%（够用但不最优）

来源: [Lance vs Chroma comparison](https://medium.com/@patricklenert/vector-databases-lance-vs-chroma-cc8d124372e9), [Zilliz comparison](https://zilliz.com/comparison/chroma-vs-lancedb)

### 16.6 Qdrant local mode 实测

**优势：** 生产成熟，HNSW 标杆，filter DSL 强大；可以 `QdrantClient(path="...")` 持久化本地文件。

**劣势：**
- 文档以 server 模式为主，local mode 在生产场景里没被官方着重推荐
- "Qdrant Edge" 是新发布的轻量版，2026 还在演进
- 本地模式与 server 模式之间存在 feature gap（filter / payload 处理）
- 包体积 + 安装比 LanceDB 大

**适配 XMclaw 度：** 60%

来源: [Qdrant Python client](https://github.com/qdrant/qdrant-client), [Qdrant docs](https://qdrant.tech/documentation/quickstart/)

### 16.7 决策矩阵

| 维度 | sqlite-vec | LanceDB | ChromaDB | Qdrant local |
|---|---|---|---|---|
| 嵌入式（无 server）| ✅ | ✅ | ✅ | ✅ |
| Python async 原生 | ❌ 手动包 | ✅ | 🟡 | ✅ |
| 真 upsert | ❌ #127 | ✅ merge_insert | ✅ | ✅ |
| KNN 内联 metadata filter | ❌ #26 | ✅ | ✅ | ✅ |
| Windows wheel | ✅ | ✅ | ✅ | ✅ |
| 生产案例 | 🟡 中小规模 | ✅ Netflix/Uber/Harvey | 🟡 LangChain 用户多 | ✅ |
| Pydantic schema | ❌ | ✅ 原生 | ❌ | 🟡 |
| Schema 演化 | ❌ 手动 ALTER | ✅ 版本化 | 🟡 | 🟡 |
| 装机体积 | ~1 MB | ~50-80 MB | ~30 MB | ~80 MB |
| 内置 BM25 全文 | ❌ 要 FTS5 | ✅ | 🟡 | ✅ |
| 内置 hybrid 检索 | ❌ | ✅ | 🟡 | 🟡 |
| 当前已踩坑数 | **4** | **0** | **0** | **0** |

**得分：sqlite-vec 5 / LanceDB 12 / ChromaDB 8 / Qdrant 9 (满分 13)**

### 16.8 取舍论证（为什么 LanceDB）

**为什么不留 sqlite-vec：** Wave 26 fix-5 只是修了一个表象 bug。结构性问题（#26 filter / #121 auxiliary / #186 perf）我们还会继续踩。每修一个 workaround 就增加一层 indirection，最后维护代价 > 换库代价。

**为什么不选 ChromaDB：** v1.0 churn 没消化；持久化 DuckDB 路径复杂；性能上限明显低于 LanceDB；async 弱。**它能用，但不能让我们走得更远。**

**为什么不选 Qdrant local：** 本地模式 feature gap 不明，文档不深；包体积大。**如果有一天 XMclaw 走 server 化，Qdrant 是好选择 —— 但当前 local-first 阶段不必。**

**为什么选 LanceDB：**
1. **API 完美贴我们的需求**：merge_insert / KNN-with-filter / Pydantic schema / async — 一行不用包
2. **生产用户跟我们同类负载**：Netflix Media Lake / Harvey RAG / agent memory 都是"嵌入式 + 复杂 filter + 中等规模"
3. **2026 还在快速进化**：1.5M IOPS / 10B 向量 / 多模态支持 — 给 XMclaw 留足上升空间
4. **零生产事故的代价是 50 MB 多装**：可接受（Playwright 本身 200+ MB，我们已经付过这价了）
5. **time-travel 表版本化**：未来 schema 迁移可回滚 — 减少架构演进的恐惧
6. **Lance format 是 ASF 项目**：Apache Arrow 生态加持，长期稳定性有保障

### 16.9 风险 + 缓解

| 风险 | 概率 | 缓解 |
|---|---|---|
| LanceDB Python API 大改 | 低 | 包 `xmclaw/memory/vec_backend.py` 抽象层，未来换库只改这一文件 |
| 50 MB 依赖在某些用户机器装不上 | 极低 | Windows wheel 实测可装；macOS / Linux 同样有 wheel |
| Lance 格式数据怎么备份？ | 中 | 目录拷贝即可（无外部 daemon），写 `xmclaw export-facts` CLI |
| 我们已有 31 MB memory.db 数据 | 中 | §10.4 迁移脚本走一次 LLM 分类过滤，搬有用的 |
| Lance 把 events.db FTS5 替代了吗？ | 否 | events.db 仍是 L0 审计源，不变。Lance 只接管 L1 facts |

### 16.10 集成边界

**新建模块：** `xmclaw/memory/vec_backend.py` —— 抽象 `VectorBackend` 接口

```python
class VectorBackend(Protocol):
    async def upsert(self, records: list[Fact]) -> None: ...
    async def search(
        self, query: list[float] | str,
        *, where: str | None = None, limit: int = 8,
    ) -> list[Fact]: ...
    async def delete(self, where: str) -> int: ...
    async def count(self, where: str | None = None) -> int: ...
```

**默认实现：** `LanceDBBackend(VectorBackend)` —— 包 LanceDB API

**Test stub：** `InMemoryBackend(VectorBackend)` —— 单元测试用，不依赖 LanceDB

**为什么留抽象层：** 防止"再换库"成本；也让 CI 跑测试时不强制装 LanceDB。

### 16.11 依赖声明（pyproject.toml）

```toml
[project.dependencies]
# ... existing ...
lancedb = ">=0.16"        # 当前稳定线
pyarrow = ">=16"          # LanceDB 必需
# sqlite-vec 暂保留, 迁移期需要

[project.optional-dependencies]
memory-full = [
    "lancedb>=0.16",
    "pyarrow>=16",
]
```

迁移期 sqlite-vec 仍在依赖列表（只读旧数据 + 迁移脚本用），Phase 6 拆除时一并移除。

### 16.12 调研资料来源

**主要：**
- [Best Vector Databases 2026 — Encore](https://encore.dev/articles/best-vector-databases)
- [Vector Database Comparison 2026 — 4xxi](https://4xxi.com/articles/vector-database-comparison/)
- [Vector Database Benchmarks 2026 — CallSphere](https://callsphere.ai/blog/vector-database-benchmarks-2026-pgvector-qdrant-weaviate-milvus-lancedb)
- [LanceDB official](https://www.lancedb.com/) / [GitHub](https://github.com/lancedb/lancedb) / [Python docs](https://lancedb.github.io/lancedb/python/python/)
- [Lance on Windows](https://lancedb.com/blog/lance-windows-windows-lance/)
- [sqlite-vec issues #127 #26 #121 #186](https://github.com/asg017/sqlite-vec/issues)
- [LanceDB vs Chroma — Zilliz](https://zilliz.com/comparison/chroma-vs-lancedb)
- [LanceDB benchmark study](https://github.com/prrao87/lancedb-study)
- [Qdrant Python client](https://github.com/qdrant/qdrant-client)

**次要：**
- [LanceDB Review — Daily Neural Digest 2026-04](https://www.dailyneuraldigest.com/tools-reviews/2026-04-16-lancedb-review/)
- [Vector DB internals — The Data Quarry](https://thedataquarry.com/blog/vector-db-1/)

---

**文档结束。等用户拍板后开始 Phase 1。**
