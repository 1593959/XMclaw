# Agent 技能系统「自主调用与语义检索」层深度调研

> 研究员：自主调用调研组
> 日期：2026-06-06
> 范围：RAG-of-tools / ToolLLM / ReAct·Toolformer / OTC·ToolRL / 2025-2026 最新进展
> 目标：为 XMclaw 技能系统的「发现层」升级提供论文依据与实现参考

---

## 0. 执行摘要

XMclaw 的自主调用流水线已完整（5 段全通），但**第 1 段「发现/surfacing」是精确瓶颈**：
- `token-overlap prefilter` 在 CJK query ↔ 英文 skill description 场景下命中归零；
- `SkillSemanticIndex`（`token + 3.0×cosine` 融合）已落地，但 embedding 召回仍是单路、无 outcome-aware 重排；
- UCB1 版本选择、`unified` 披露、meta-tool 兜底均已对齐 SOTA。

**本调研结论**：XMclaw 应优先做三件事——(1) 把语义召回从「单路 cosine」升级为「hybrid 语义 + outcome-aware 重排」；(2) 引入 DFSDT 式多步决策树处理多工具组合任务；(3) 用 OTC-PO 式「工具生产力」指标替代纯成功率，抑制过度调用。

---

## 1. RAG-of-tools / 语义路由（发现层）

### 1.1 论文与出处

| 工作 | 论文 | 核心数字 | 关键洞察 |
|------|------|----------|----------|
| **RAG-MCP** | arXiv:2505.03275 (2025-05-06) | 准确率 43.13% vs 13.62% baseline（3.2×）；prompt token 从 2134 → 1084（−49%） | 把全部 MCP schema 塞进 prompt 会让 LLM「看不见」正确工具；语义检索先召回 top-1 schema 再交给 LLM，决策边界更清晰 |
| **Toolshed** | arXiv:2410.14594v2 | 多跳查询检索准确率 95-100%（工具数 <500），>500 时 80-100% | Advanced RAG-Tool Fusion：不是平面 top-k，而是「种子技能 → 结构感知扩展 → 依赖补齐」 |
| **OATS** | arXiv:2603.13426 (2026-03-13) | MetaTool NDCG@5 从 0.869 → 0.940；ToolBench 0.834 → 0.848 | Outcome-Aware Tool Selection：离线把工具 embedding 向「该工具历史上成功的查询质心」插值；零参数、零延迟、零 GPU |
| **Graph-of-Skills** | arXiv:2604.05333v3 (2026-05-27) | 解决「语义近但功能不足」的 prerequisite gap | 技能不是平面列表，而是有向图；向量召回后做结构感知扩展，把 prerequisite 技能（如 parser、converter）一并打包 |
| **Quantitative Certification** | arXiv:2510.03992v2 (2026-05-13) | 形式化验证工具选择管线的鲁棒性 | 开放注册表（MCP）中工具池由第三方塑造，存在近重复、错误标签、权限提升攻击；需要量化认证 |

### 1.2 真实源码与实现

**RAG-MCP 官方实现**（概念验证级）：
- GitHub: `memoverflow/rag-mcp` — `knowledge_base.py` 模块实现语义检索核心
- 论文实验使用 qwen-max-0125 驱动，Deepseek-v3 当 evaluator
- 三阶段管线：Tool Indexing → Query-Based Retrieval → Focused LLM Processing

**OATS 生产级语义路由器**（vLLM Semantic Router 团队）：
- 论文出自 vLLM Semantic Router Team + CMU/UMN 合作
- 核心代码思路（伪代码，来自论文 §3.2）：

```python
# OATS: 离线 outcome-aware 插值 —— 零服务时开销
def interpolate_embedding(tool_emb, success_query_centroid, alpha=0.15):
    """把工具原始描述 embedding 向「成功查询质心」拉 15%。"""
    return normalize((1 - alpha) * tool_emb + alpha * success_query_centroid)

# 在线检索：纯 CPU、单数位毫秒
retrieved = vector_index.search(query_emb, top_k=K)
# 可选：2,625 参数 MLP re-ranker（仅当 outcome 数据密度足够时启用）
```

**Toolshed 的 RAG-Tool Fusion**（来自论文 Fig.4/Fig.5）：
- 对比 Seal-Tools DPR：工具数增加时 DPR 准确率显著下降，而 Advanced RAG-Tool Fusion 在 500+ 工具时仍保持 80-100% top-k<6 准确率
- 关键差异：DPR 只做平面语义相似度；RAG-Tool Fusion 在召回后做「多跳依赖扩展」

### 1.3 与 XMclaw 的对比

| 维度 | 头部做法 | XMclaw 现状 | 差距 |
|------|----------|-------------|------|
| 召回信号 | RAG-MCP：纯语义向量召回；OATS：outcome-aware 插值 | `SkillSemanticIndex` 复用 `EmbeddingService`，`token + 3.0×cosine` 融合 | 🟡 已有语义召回，但缺 outcome-aware 重排 + 多路 hybrid |
| 多工具组合 | Toolshed/GoS：结构感知扩展，自动补齐 prerequisite | 扁平列表，无显式依赖图 | 🟡 缺「技能图」结构 |
| 生产延迟 | OATS：单数位毫秒 CPU 预算 | `warm()` 后台 fire-and-forget，热路径只做一次 query embed + 内存 cosine 扫描 | 🟢 延迟已对齐 |
| 安全/认证 | Quantitative Certification：形式化验证工具选择 | 注入扫描 + `allowed_tools` 解析（运行时尚未强制） | 🟡 缺量化认证 |

**XMclaw 的具体代码**（`xmclaw/skills/semantic_index.py`）：

```python
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

**融合点**（`xmclaw/skills/prefilter.py` §⑫）：

```python
_SEMANTIC_WEIGHT = 3.0
# 在 _score_skill 之后叠加语义信号
if semantic_scores:
    _sem = semantic_scores.get(getattr(spec, "name", "") or "", 0.0)
    if _sem > 0:
        s += _SEMANTIC_WEIGHT * _sem
```

**建议**：XMclaw 的 `SkillSemanticIndex` 已经是 RAG-of-tools 的「最小可用实现」。下一步：
1. 引入 OATS 的 outcome-aware 插值——利用 `registry.record_usage()` 已有的 success/failure 统计，计算每个技能的「成功查询质心」；
2. 当技能数 >100 时，从平面列表切换到「语义种子 + 路径条件扩展」（借鉴 Toolshed）；
3. 对 MCP 接入的工具池启用 Quantitative Certification 思路——第三方工具需通过描述一致性校验。

---

## 2. ToolLLM / ToolBench（大规模 API 选择与多步决策）

### 2.1 论文与出处

- **ToolLLM: Facilitating Large Language Models to Master 16000+ Real-world APIs**
  - 论文：arXiv:2307.16789 (2023-07-31)，**ICLR 2024 Spotlight**
  - 作者：Yujia Qin 等，清华 OpenBMB 团队
  - 核心数字：16,464 真实 RESTful APIs，49 类别，12,000+ 任务实例

**关键组件**：
1. **ToolBench**：自动构建的指令微调数据集（ChatGPT 生成指令 + 解决方案路径）
2. **神经 API 检索器**：Sentence-BERT，BERT-BASE 密集检索器；指令和 API 文档分别编码为两个嵌入向量，通过对比学习训练
3. **DFSDT（Depth-First Search-Based Decision Tree）**：让 LLM 评估多条推理轨迹并扩展搜索空间
4. **ToolEval**：基于 ChatGPT 的自动评估器（pass rate / win rate）
5. **ToolLLaMA**：LLaMA 7B 在 ToolBench 上微调，positional interpolation 扩展上下文到 8192

### 2.2 真实源码与实现

**官方仓库**：`github.com/OpenBMB/ToolBench`（Apache-2.0）

**神经 API 检索器训练**（来自论文 §3.2 与开源实现）：

```python
# toolbench/retrieval/train_retriever.py 思路（来自论文与社区复现）
from sentence_transformers import SentenceTransformer, InputExample, losses

# 双塔模型：指令塔 + API 文档塔
model = SentenceTransformer('bert-base-uncased')

# 正样本：指令与其相关 API 文档
# 负样本：随机采样其他 API
train_examples = [
    InputExample(texts=[instruction, api_doc], label=1.0),
    InputExample(texts=[instruction, unrelated_api], label=0.0),
]
train_loss = losses.MultipleNegativesRankingLoss(model)
```

**DFSDT 推理算法**（来自论文 §3.3 与开源推理脚本）：

```python
# toolbench/inference/qa_pipeline.py 核心逻辑（社区复现版）
def dfsdt_solve(query, apis, max_depth=5):
    """深度优先搜索决策树：LLM 在每个节点生成候选动作，
    评估后继续深入或回溯。"""
    stack = [(query, [], 0)]  # (current_state, action_chain, depth)
    best_solution = None
    while stack:
        state, chain, depth = stack.pop()
        if depth > max_depth:
            continue
        # LLM 生成候选 API 调用
        candidates = llm.generate_candidates(state, apis)
        for action in candidates:
            new_state = execute_api(action)
            if is_terminal(new_state):
                return chain + [action]
            stack.append((new_state, chain + [action], depth + 1))
    return best_solution
```

**服务端推理入口**（来自官方 README）：

```bash
python toolbench/inference/toolbench_server.py \
    --tool_root_dir data/toolenv/tools/ \
    --corpus_tsv_path data/retrieval/G1/corpus.tsv \
    --retrieval_model_path /path/to/your/retrival_model \
    --retrieved_api_nums 5 \
    --backbone_model toolllama \
    --method DFS_woFilter_w2
```

### 2.3 与 XMclaw 的对比

| 维度 | ToolLLM | XMclaw 现状 | 差距 |
|------|---------|-------------|------|
| 规模 | 16K APIs，神经检索器 + DFSDT 多步决策 | ~400 skills，`token+embedding` prefilter → top-12 | 🔴 规模与决策深度差距大 |
| 检索器 | 专用 Sentence-BERT 双塔，对比学习训练 | 通用 `EmbeddingService`（复用记忆系统的 embedder） | 🟡 通用 embedder 够用，但缺「指令-工具」双塔微调 |
| 多步组合 | DFSDT：显式搜索多条工具链，评估后选择 | 单步 `skill_run`，组合发生在 LLM 编排层 | 🟡 缺显式多步决策树 |
| 评估 | ToolEval：ChatGPT-based judge（pass/win rate） | HonestGrader：确定性 ground-truth 检查 | 🟢 XMclaw 评估更诚实 |
| 训练数据 | 自动构建 12K 任务实例 | 无专用工具使用训练集 | 🟡 缺大规模工具使用 SFT 数据 |

**关键洞察**：ToolLLM 证明了一件事——当工具数 >100 时，**平面 top-k 语义召回不够**，需要：
1. 专用检索器（双塔对比学习 > 通用 embedding）；
2. 显式多步决策（DFSDT > 单步 model-invoked）；
3. 训练数据覆盖（ToolBench > 零样本提示）。

XMclaw 当前在 (1) 上用了通用 embedder（可接受），在 (2) 和 (3) 上存在缺口。但 XMclaw 的 UCB1 版本选择 + HonestGrader 评估是 ToolLLM 没有的独特优势。

---

## 3. ReAct / Toolformer（自主调用的基础范式）

### 3.1 论文与出处

| 工作 | 论文 | 核心贡献 | 自主调用机制 |
|------|------|----------|--------------|
| **ReAct** | arXiv:2210.03629 (2022-10)，ICLR 2023 | Thought → Action → Observation 交错循环 | **Prompt 级**：模型在生成中显式输出推理轨迹（Thought），再决定调用哪个工具（Action） |
| **Toolformer** | arXiv:2302.04761 (2023-02)，NeurIPS 2023 | LLM 自监督学习使用外部工具 | **权重级**：在预训练/微调阶段把「何时调工具」训进模型权重，推理时自动触发 API token |

**ReAct 核心发现**（论文 §4）：
- 在 HotpotQA（多跳问答）上，ReAct 超越纯 Chain-of-Thought（CoT）和纯 Action-only；
- 在 AlfWorld（决策任务）上，ReAct 的交互式探索使成功率从 71% 提升到 91%；
- **关键洞察**：显式推理轨迹让模型能「计划 → 行动 → 根据观察调整计划」，这是自主调用的认知基础。

**Toolformer 核心发现**（论文 §3）：
- 使用少量人工标注的 API 调用示例，通过自监督方式在大量文本中插入 `<API>` token；
- 模型学会：何时调用（任务需要计算/检索时）、调用哪个（从可用 API 中选）、如何解析结果；
- 在数学、时间、问答任务上，7B 模型 + Toolformer 超越 175B 纯文本模型。

### 3.2 真实源码与实现

**ReAct 的极简实现**（LangChain 社区版，与论文逻辑一致）：

```python
# lang

---

## 4. OTC / ToolRL（过度调用惩罚与奖励塑形）

### 4.1 论文与出处

| 工作 | 论文 | 核心数字 | 关键洞察 |
|------|------|----------|----------|
| **OTC-PO** | arXiv:2504.14870 (2025-04-21) | 工具调用减少 68.3%；工具生产力提升 215.4%；准确率持平 | Optimal Tool Call-controlled Policy Optimization：把「答案正确性」和「工具使用效率」同时纳入 RL 奖励 |
| **ToolRL** | Qian et al., 2025 (arXiv:2501.00316) | 奖励分解为 format + correctness | 在合成工具使用数据集上应用 RL，format reward 保证调用格式正确，correctness reward 保证结果正确 |
| **COVERT** | arXiv:2604.09813 (2026) | 将可靠轨迹转化为 oracle-preserving 训练环境 | 系统性控制查询歧义、工具集干扰项、工具输出扰动，用于 rollout-time reward 计算 |
| **ToolZero** | Zeng et al., 2025 | 动态从宽松到严格的 correctness reward | 训练初期允许部分正确，后期严格要求，渐进式塑形 |

**OTC-PO 的核心公式**（来自论文 §3）：

```
R_total = R_correctness + λ · R_tool_efficiency

其中 R_tool_efficiency = −(num_tool_calls / max_allowed_calls)

工具生产力 Tool Productivity = #correct_answers / #total_tool_calls
```

**关键洞察**：现有 RL 方法（如 PPO/GRPO）通常只优化最终答案正确性，导致模型「认知卸载」——能查就查、能算就算，过度依赖外部工具。OTC-PO 通过联合奖励，鼓励模型「先自己想想，必要时才调用工具」。

### 4.2 真实源码与实现

**OTC-PO 的奖励计算**（论文 §3.2，伪代码）：

```python
# OTC-PPO / OTC-GRPO 的奖励塑形（来自论文算法描述）
def compute_otc_reward(answer_correct: bool, num_tool_calls: int,
                       max_calls: int = 10, lambda_eff: float = 0.5):
    r_correct = 1.0 if answer_correct else 0.0
    # 效率惩罚：调用越多惩罚越大，但保证非负
    r_eff = max(0.0, 1.0 - num_tool_calls / max_calls)
    return r_correct + lambda_eff * r_eff

# Group Relative Preference Optimization (GRPO) 变体
# 对同一问题的多个输出采样，按相对奖励排序
```

**ToolRL 的奖励分解**（来自 COVERT 论文 §2.2 引用）：

```python
# ToolRL (Qian et al., 2025) 奖励结构
def toolrl_reward(trajectory, ground_truth):
    r_format = check_json_schema(trajectory.tool_calls)  # 格式正确？
    r_correct = check_execution_result(trajectory, ground_truth)  # 结果正确？
    return r_format + r_correct
```

**COVERT 的环境控制**（论文 §3）：

```python
# COVERT: 可控工具使用训练环境
def make_covert_env(trajectory, tool_set, noise_level=0.1):
    """将可靠轨迹转化为训练环境，注入：
    1. 查询歧义（query ambiguity）
    2. 工具集干扰项（distractor tools）
    3. 工具输出扰动（output perturbation）
    """
    env = ToolUseEnv(base_trajectory=trajectory)
    env.add_distractors(n=len(tool_set) * noise_level)
    env.perturb_outputs(level=noise_level)
    return env
```

### 4.3 与 XMclaw 的对比

| 维度 | OTC / ToolRL | XMclaw 现状 | 差距 |
|------|--------------|-------------|------|
| 过度调用惩罚 | OTC-PO：联合奖励，显式效率项 | `unified` 披露模式（超阈值砍 per-skill 工具）+ prefilter 收窄 | 🟡 系统级收窄有，但缺「每次调用的效率评估」 |
| 奖励塑形 | ToolRL：format + correctness 分解 | HonestGrader：`check_ran` / `check_returned` / `check_type_matched` / `check_side_effect_observable` | 🟢 XMclaw 评估更细粒度，但未用于在线 RL 塑形 |
| 在线学习 | COVERT：可控环境 + rollout reward | UCB1 `VariantSelector` 在线探索版本 | 🟡 UCB1 是 bandit 级，缺 MDP 级 RL |
| 指标 | Tool Productivity = 正确数 / 调用数 | `success_rate` + `avg_latency_ms` | 🟡 缺「单位工具调用的产出效率」指标 |

**XMclaw 的 UCB1 版本选择**（`xmclaw/skills/variant_selector.py`）：

```python
class VariantSelector:
    def pick_version(self, skill_id: str) -> int | None:
        # HEAD warm-up：先给 HEAD 一些 plays 建立基线
        head_stats = self._stats.get((skill_id, head))
        if head_stats is None or head_stats.plays < self.head_warmup_plays:
            return head
        # UCB1：explore 未充分测试的候选版本
        for v in versions:
            arm = self._stats.get((skill_id, v))
            if arm is None or arm.plays == 0:
                return v  # 未探索 → 无限 UCB，直接选
            score = arm.mean + self.exploration_c * math.sqrt(log_total / arm.plays)
```

**建议**：XMclaw 的 UCB1 已经是「在线探索」的最小可用实现。下一步：
1. 引入 `tool_productivity` 指标到 `SkillUsageStats`——不只是 `success_rate`，而是「每次调用解决了多少问题」；
2. 把 HonestGrader 的确定性检查结果作为 RL 奖励信号（而非仅用于晋升门控）；
3. 对 `skill_browse` 的过度使用做软性惩罚——如果 agent 连续 browse 但从不 run，降低 browse 的 UCB 分数。

---

## 5. 2025-2026 最新进展

### 5.1 上下文学习与神经检索

| 工作 | 出处 | 核心创新 | 与技能发现的相关性 |
|------|------|----------|-------------------|
| **In-Context Tool Learning** | arXiv:2503.21460 (Survey) | 把工具使用示例作为 few-shot 上下文，无需微调 | XMclaw 的 `skill_view` 已把 SKILL.md 正文当「上下文示例」喂给 agent，天然对齐 |
| **Topic Coverage-based Demo Retrieval** | Kweon et al., EMNLP 2025 | 按主题覆盖度检索演示示例，而非简单 top-k 相似度 | 可借鉴到技能演示检索：不只召回最像的，还要覆盖任务的不同方面 |
| **Contextual Experience Replay** | Liu et al., ACL 2025 | 智能体从过去经验中选择性回放，提升自我改进 | XMclaw 的 episode/lesson 记忆可升级为「经验回放池」 |
| **SimpleMem** | Liu et al., arXiv:2601.02553 | 高效终身记忆架构 | 技能使用历史可作为终身记忆的一部分 |

### 5.2 强化学习与工具选择

| 工作 | 出处 | 核心创新 |
|------|------|----------|
| **COVERT** | arXiv:2604.09813 | 将可靠轨迹转化为 oracle-preserving 训练环境，系统性控制查询歧义、干扰项、输出扰动 |
| **Reinforcement Pre-Training** | Dong et al., 2025 | 把 next-token prediction 重构为 RL 问题，用可验证奖励 |
| **avataRL** | tokenbender, 2025 | 从随机初始化纯用 RL 训练语言模型 |
| **L-CPO / THINKPRUNE** | Hou et al., 2025; Aggarwal & Welleck, 2025 | 长度约束策略优化，控制推理长度 |

### 5.3 生产系统与 MCP 生态

| 工作 | 出处 | 核心创新 | 与 XMclaw 的相关性 |
|------|------|----------|-------------------|
| **mcp-compressor** | Atlassian Labs, 2026 | 用两个通用 wrapper 工具（get_tool_schema / invoke_tool）替代全部工具清单，token 减少 70-97% | XMclaw 的 `unified` 披露模式（只留 `skill_run`）已是同类思路 |
| **TSCG** | arXiv:2605.04107 | TypeScript Schema Compression Grammar，保留完整语义的同时压缩 token | 可用于 SKILL.md 参数描述的压缩 |
| **SIRP** | Chen & Jalil, IETF 2025 | Semantic Inference Routing Protocol，语义推理路由协议 | 未来 MCP 网关的标准化方向 |
| **RCR-Router** | Liu et al., arXiv:2508.04903 | 角色感知上下文路由，多 agent 系统的结构化记忆路由 | XMclaw 的 `ModeRouter` 可扩展为角色感知路由 |

### 5.4 与 XMclaw 的对比总结

XMclaw 在以下维度**已对齐或领先** 2025-2026 SOTA：
- ✅ **渐进披露**：`inline/unified/auto` 模式 = Anthropic 3 级披露 = mcp-compressor 的 wrapper 思路
- ✅ **评估诚实性**：HonestGrader 确定性检查 > LLM-as-judge（被 survey 列为开放难题）
- ✅ **版本探索**：UCB1 bandit > 静态 benchmark 分数
- ✅ **元工具兜底**：`skill_browse` 永远白名单 = RAG-MCP 的「检索失败 fallback」

XMclaw 在以下维度**存在可追赶的缺口**：
- 🟡 **Outcome-aware 重排**：OATS 的零成本插值可直接嫁接
- 🟡 **技能依赖图**：Graph-of-Skills 的 prerequisite 扩展可逐步引入
- 🟡 **工具生产力指标**：OTC-PO 的联合奖励可纳入 UCB1 的 score 计算
- 🟡 **多步决策树**：DFSDT 对复杂多技能任务的价值
- 🔴 **专用训练数据**：ToolBench 式的大规模工具使用 SFT 数据集
