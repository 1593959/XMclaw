# XMclaw 自主进化系统

XMclaw 的自主进化系统是其核心差异化能力。它让 Agent 能够从对话中**自动学习**，生成可执行的 **Gene（基因/行为模式）** 和 **Skill（工具技能）**，并持续自我改进。

---

## 进化闭环

```
对话历史
    │
    ▼
┌─────────────────┐
│   OBSERVE       │  观察用户行为模式
│   (观察)        │  意图统计、趋势分析、洞察提取
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│    LEARN        │  从模式中学习
│   (学习)        │  生成 Gene / Skill 设计
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│    EVOLVE       │  代码生成
│   (进化)        │  GeneForge / SkillForge 生成 Python 代码
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│    VALIDATE     │  真实运行验证
│   (验证)        │  py_compile + importlib + 实例化 + execute()
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│    SOLIDIFY     │  固化注册
│   (固化)        │  注册到 GeneManager / ToolRegistry
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│    RELOAD       │  热重载
│   (重载)        │  立即生效，无需重启
└─────────────────┘
```

---

## Gene 系统

### 什么是 Gene？

Gene 是**行为的抽象模板**。它定义了在特定情境下 Agent 应该如何调整自己的系统提示词或行为策略。

例如：
- `gene_proactive_retrieval`: 在搜索前优先查记忆
- `gene_error_repair`: 遇到错误时自动尝试修复

### Gene 结构

```python
class Gene:
    gene_id: str
    name: str
    description: str
    trigger: dict      # 触发条件（关键词、意图、正则）
    prompt_addition: str   # 注入到 system prompt 的文本
    priority: int      # 优先级，高优先级先注入
```

### Gene 注入流程

1. 用户发送消息
2. `GeneManager.match(user_input)` 根据触发条件匹配 Gene
3. `PromptBuilder` 将匹配到的 Gene 的 `prompt_addition` 注入 system prompt
4. LLM 在生成回复时自然遵循这些行为模式

---

## Skill 系统

### 什么是 Skill？

Skill 是**可执行工具的代码封装**。与 Gene 不同，Skill 直接扩展 Agent 能做的事情。

例如：
- `auto_entity_reference_v26`: 自动提取用户输入中的文件路径、URL、邮箱
- `auto_repair_v30`: 自动修复跨会话问题

### Skill 生成流程

1. `SkillForge` 分析洞察（高频需求、用户痛点）
2. 使用 LLM 生成完整的 Python Tool 子类代码
3. `EvolutionValidator` 验证代码：
   - `py_compile` 语法检查
   - `importlib` 动态导入
   - 实例化 Tool 子类
   - 调用 `execute()` 真实运行
4. 保存到 `shared/skills/skill_{name}.py`
5. `ToolRegistry` 热重载，立即可用

### Skill 版本管理

- 不限制版本数量，每次在最新版本基础上迭代
- 自动清理旧版本，只保留最近 2 个版本
- 避免文件无限堆积

---

## 进化触发条件

进化不是每次对话都运行，而是满足以下条件时才触发：

1. **对话数量阈值**: 积累足够多的新对话
2. **时间间隔**: 距离上次进化 ≥ 30 分钟
3. **模式检测**: 检测到新的用户行为模式
4. **VFM 评分**: 进化产出的价值评分必须超过阈值

---

## VFM（价值函数模型）

VFM 用于评估一次进化是否"值得"固化：

| 评分维度 | 说明 |
|---------|------|
| 新颖性 | 是否解决了以前没有覆盖到的问题 |
| 通用性 | 是否能应用到多种场景 |
| 可验证性 | 生成的代码是否能通过真实运行验证 |
| 简洁性 | 实现是否简洁，不引入过度复杂度 |

VFM 总分 ≥ 阈值（默认 30/100）时，进化产物才会被固化。

---

## 洞察提取

进化引擎从对话中提取以下类型的洞察：

- **高频意图**: 用户反复请求某类操作
- **未满足需求**: 用户表达了 Agent 当前做不到的事
- **负面反馈**: 用户纠正或不满
- **成功模式**: 用户认可的有效行为

这些洞察会同时保存到：
- `shared/memory.db` 的 `insights` 表
- `agents/{agent_id}/MEMORY.md` 的长期记忆

---

## 查看进化状态

### 桌面端
进入侧边栏「进化」视图，可查看：
- Genes 列表
- Skills 列表
- Insights（洞察）

### CLI
```bash
xmclaw evolution-status
```

### Web UI
在 Agent OS 仪表盘的 Evolution 面板中查看。
