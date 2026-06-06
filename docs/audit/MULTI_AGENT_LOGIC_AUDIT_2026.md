# 多 Agent 群聊/工作流 —— 逻辑判错 + 正确范式调研（2026-06-06）

> 用户反馈"逻辑全都有问题，先去调研论文/实现"。本文扒了 AutoGen / Magentic-One /
> CrewAI / LangGraph 的**真实实现机制**，照出 XMclaw 当前实现（G1~G4 我刚写的）的
> 逻辑硬伤，给出正确重做方向。**结论：群聊和工作流两条都没按正确范式做，要重写编排核心。**

---

## A. 正确范式（真实源码/论文）

### A1. AutoGen GroupChat（群聊的标准做法）
发布-订阅模型：**所有 agent 订阅共享话题，各自累积「完整」会话历史**（每条消息都收到、
`handle_message` 追加到本地 history）。`GroupChatManager` 循环：
**收消息 → 追加历史 → LLM 选下一个讲者（prompt = 参与者[排除上一个发言者] + 完整历史 →
选一个角色）→ 给该 agent 发 RequestToSpeak**。只有被选中的 agent 这一轮**发言**，
其余只**听**（更新历史）。终止：收到约定信号（如 "approve"）。
> 源：<https://microsoft.github.io/autogen/stable//user-guide/core-user-guide/design-patterns/group-chat.html>，论文 <https://arxiv.org/pdf/2308.08155>

### A2. Magentic-One Orchestrator（有目标工作流的标准做法）
**双台账 + 双循环**：
- **任务台账(Task Ledger)**：已知/已验证事实、待查事实、待推导事实、有根据的猜测。
- **进度台账(Progress Ledger)**：当前进度、子任务分给谁、对编排问题的回答。
- **外循环**：建任务台账 → 生成计划(步骤+分派) → 进内循环；**卡住就回外循环反思重规划**。
- **内循环每步问 5 个问题**：①任务完成没？②团队在打转/重复没？③有前进没？
  ④下一个谁发言？⑤给他什么指令？
- **卡住检测**：计数器超阈值 → 跳出内循环、重规划。终止：完成 / 达上限。
> 源：<https://www.microsoft.com/en-us/research/articles/magentic-one-a-generalist-multi-agent-system-for-solving-complex-tasks/>，论文 <https://arxiv.org/html/2411.04468v1>

### A3. CrewAI Hierarchical（角色化分层）
**manager_llm 必填**；manager 读每个 agent 的 **role/goal/backstory** 决定派给谁、什么顺序、
给什么输入。**已知坑（社区 issue）**：role/goal 写不清 → manager 乱派 / "总是派给所有 agent"。
> 源：<https://docs.crewai.com/en/learn/hierarchical-process> · 社区 bug
> <https://community.crewai.com/t/hierarchical-process-always-delegates-to-all-agents-irrespective-of-the-categorization/3171>

### A4. LangGraph Supervisor（确定性交接）
`Command(goto="agent", update={...})` 把**控制流 + 共享状态更新**绑在一起做 handoff；
supervisor **不指名下一个 worker 时即终止**（终止逻辑写进 supervisor prompt）。
> 源：<https://reference.langchain.com/python/langgraph-supervisor>

**共识**：① 共享完整历史/状态；② 一个 LLM 编排者(manager/orchestrator/supervisor)按
**结构化角色**选下一个；③ 工作流要**迭代 + 进度跟踪 + 卡住重规划**，不是一把梭；④ 明确终止。

---

## B. XMclaw 当前实现的逻辑硬伤（对照上面）

### 群聊（`group_orchestrator.py` + `routers/rooms.py`）
1. **❌ 历史模型错**：我每回合**清空 agent 的 history，再把整段 transcript 当「一条 user
   消息」塞进去**。AutoGen 是每个 agent **累积真实多轮历史**。我的做法让 agent 把整场对话
   看成"用户一次性说的一大段"→ 角色/轮次错乱、回应不连贯。
2. **❌ 主持人 LLM 选讲者根本没接**：`rooms.py` 调 `GroupOrchestrator` 时**没传 llm_select**
   → supervisor 策略静默退化成 round_robin。用户要的"主持人 LLM 自动"**完全没生效**。
3. **❌ 无终止判断**：只有 max_rounds 死循环上限，没有 A1/A2 的"任务完成没/在打转没"。
4. **❌ 选讲者不排除上一个发言者**（round_robin 纯取模），易出现同一 agent 连说或空转。

### 工作流（`workflow_room.py` + `SwarmOrchestrator`）
5. **❌ 无视房间参与者**：`LoadBalancer.assign` 枚举 `manager.list_ids()`=**全局所有 agent**，
   不是房间选的参与者（= CrewAI 那个著名 bug 同款）。房间里"拉谁进来"对工作流毫无作用。
6. **❌ 一把梭，无 Magentic-One 迭代**：SwarmOrchestrator = 分解→派发→聚合**一次性**，
   **没有进度台账、没有内循环 5 问、没有卡住重规划**。某步失败/跑偏无法自纠。
7. **❌ 脆弱依赖**：`TaskScheduler` 没 `executor` → 所有任务 "no executor configured" 全失败；
   `HTNPlanner.plan` 需 LLM，失败即 ok=False。这两条任一没接好，工作流房间**整体跑不出东西**
   （需在真机验证 executor 是否真按 task.agent_id 路由到对应 agent 的 run_turn）。
8. **❌ 能力匹配靠子串**：`_match_capability` 只按 agent_id 里有没有 "dev"/"research" 等英文
   子串——中文/自定义 id 全 miss，退化成轮询。

### 记忆互通
9. **⚠ 越界**：我把参与者 `_memory_service` 指到**主 agent 的全局库**→ 是"互通"了，但所有
   房间 agent 读写**用户主记忆**，会污染主库；应是**房间级共享库**，不是劫持主库。

---

## C. 正确重做方向（建议）

### C1. 群聊 → 照 AutoGen 重写编排核心
- **共享历史**：Orchestrator 持房间消息列表；每回合给被选 agent 一个**真实多轮 messages**
  （他人发言作 assistant/named 角色，而非塞成一条 user blob）。需要 run_turn 支持注入
  历史，或给群聊用一条更底层的 LLM 调用（绕过单 agent 的 history 累积）。
- **真·LLM 选讲者**：在 `rooms.py` 注入 `llm_select`（用主 agent 的 `_llm`），prompt 含
  参与者角色简介 + 排除上一个发言者 + 近况 → 选下一个或 USER/DONE。
- **终止**：选讲者可输出 DONE；加"是否已回答用户"判断。

### C2. 工作流 → 照 Magentic-One 写真编排器（不要只靠 SwarmOrchestrator 一把梭）
- 新 `RoomWorkflowOrchestrator`：**任务台账 + 进度台账**，**内循环 5 问**(完成/打转/进展/
  谁下一个/给什么指令)，**卡住计数→重规划**。
- **限定在房间参与者**内分派（修 #5：不要用全局 list_ids，传入 participants）。
- 每步调 `participant.run_turn`，事件流到房间 session（前端实时看）。
- 可继续复用 HTNPlanner 做初始分解，但**执行/进度/重规划走新编排器**，而非 scheduler 一次性。
- 先验证 #7（executor 是否真跑 agent）；若没接好，编排器自己直接调 run_turn 更可控。

### C3. 结构化人格（C1/C2 的前提）
给 agent config 加 **role/goal/backstory**（CrewAI 式）——选讲者/分派器靠这个判断"谁该上"。
没有它，LLM 编排者没法正确选人（CrewAI 的坑根因）。

### C4. 记忆
房间级共享 MemoryService（按 room_id scope），不要劫持主库。

---

## D. 取舍 / 给用户的决策点
1. **群聊历史**：要不要给 run_turn 加"注入外部 messages"入参（较干净），还是群聊另走一条
   底层 LLM 调用？
2. **工作流执行**：复用 TaskScheduler（先验证 executor），还是新编排器**直接调 run_turn**
   （更可控、更像 Magentic-One）？建议后者。
3. 当前 G1~G4 已合并的代码：**保留外壳(房间/面板/UI/CRUD)**，**重写编排核心**(group_orchestrator
   + workflow_room) —— 外壳没问题，是内核逻辑要换成上面的正确范式。

## Sources
- AutoGen GroupChat 设计模式 · 论文 2308.08155
- Magentic-One 文章 + 论文 2411.04468
- CrewAI Hierarchical 文档 + 社区"派给所有 agent"bug
- LangGraph Supervisor / Command handoff
（URL 见上文各处）
