---
name: xm-auto-evo
description: "XM-AUTO-EVO 完全自动进化系统。观察用户行为 → 检测模式 → 自动生成 Gene/Skill → 持续进化。集成 qwen3-embedding:0.6b 向量记忆。"
metadata:
  copaw:
    emoji: "🧬"
---

# 🧬 XM-AUTO-EVO - 完全自动进化系统

> 基于 CoPaw + XM-EVO，借鉴 Hermes 三层记忆理念  
> **向量模型**: qwen3-embedding:0.6b (Ollama)

## 什么时候用

- 用户希望 AI **自主学习、自我进化**
- 需要**自动检测用户行为模式**
- 需要**自动生成 Gene 或 Skill** 来适应新需求
- 想**躺平**让系统自己变强
- 用户提到"自动进化"、"自我学习"、"自动生成能力"

## 核心功能（已补全）

| 功能 | 状态 | 说明 |
|------|------|------|
| **ADL 安全层** | ✅ 完整 | 劣化检测 + 回滚机制 + 反进化锁 |
| **自动观察层** | ✅ 完整 | Session 信号提取 + 模式识别 |
| **三层记忆系统** | ✅ 完整 | 短时/中时/长时 + Ollama 向量搜索 |
| **自动 Gene 生成** | ✅ 完整 | 根据重复模式自动创建 |
| **自动 Skill 创建** | ✅ 完整 | 生成 Skill 框架文件 |
| **能力树扩展** | ✅ 完整 | 自动节点创建与维护 |
| **调度器** | ✅ 完整 | 自适应间隔心跳 |
| **进化选择器** | ✅ 完整 | 基于信号的 Gene 匹配 |

## 系统架构

```
┌──────────────────────────────────────────────────┐
│              🔍 自动观察层                         │
│  signals.js      pattern.js     conversation.js   │
└────────────────────────┬─────────────────────────┘
                         ▼
┌──────────────────────────────────────────────────┐
│              🧠 三层记忆系统                       │
│  short.js    medium.js    long.js    vector.js   │
│                              (qwen3-embedding)   │
└────────────────────────┬─────────────────────────┘
                         ▼
┌──────────────────────────────────────────────────┐
│              ⚙️ 自动进化层                         │
│  gene_forge.js  skill_maker.js  tree.js          │
│  selector.js   strategy.js   personality.js     │
└────────────────────────┬─────────────────────────┘
                         ▼
┌──────────────────────────────────────────────────┐
│              🛡️ ADL 安全层                         │
│  validator.js  rollback.js  lock.js              │
└──────────────────────────────────────────────────┘
```

## CLI 命令

```bash
# 启动完整进化循环（观察+学习+进化）
node skills/xm-auto-evo/index.js start

# 单次观察分析
node skills/xm-auto-evo/index.js observe

# 单次学习阶段
node skills/xm-auto-evo/index.js learn

# 单次进化阶段
node skills/xm-auto-evo/index.js evolve

# 生成进化建议
node skills/xm-auto-evo/index.js suggest

# 查看系统状态
node skills/xm-auto-evo/index.js status

# 查看记忆状态
node skills/xm-auto-evo/index.js memory status

# 语义搜索记忆（需 Ollama）
node skills/xm-auto-evo/index.js memory search <内容>

# 查看已有 Gene
node skills/xm-auto-evo/index.js genes

# 启动心跳模式（定时自动循环）
node skills/xm-auto-evo/index.js heartbeat

# 查看帮助
node skills/xm-auto-evo/index.js help
```

## 自动进化流程

```
每小时 HEARTBEAT 执行：
1️⃣ 观察阶段 → 从 session 提取信号，检测模式
2️⃣ 学习阶段 → 三层记忆更新，向量存储
3️⃣ 进化阶段 → 检查 ADL 安全层，自动生成 Gene/Skill
4️⃣ 验证阶段 → 记录 Capsule，生成事件日志
```

## Gene 自动生成条件

| 条件 | 默认值 |
|------|--------|
| 同一模式出现次数 | ≥ 3 次 |
| 每日最大 Gene 数 | 5 |
| V-Score 安全门槛 | 50 |

## 向量记忆配置

```json
{
  "vector": {
    "ollama_base": "http://localhost:11434",
    "model": "qwen3-embedding:0.6b",
    "enabled": true,
    "dimension": 1024,
    "top_k": 5
  }
}
```

**前提条件**：Ollama 已安装并运行，模型已拉取：
```bash
ollama pull qwen3-embedding:0.6b
```

## 配置关键项

- `auto_evolution.enabled` - 是否启用自动进化
- `auto_evolution.evolution_trigger_threshold` - 触发进化的模式重复次数
- `scheduler.interval_min` - 心跳间隔（分钟）
- `vector.model` - Ollama embedding 模型
- `pattern_detection.forbidden_patterns` - 禁止检测的敏感词

## 与 XM-EVO 的关系

| | XM-EVO | XM-AUTO-EVO |
|--|--------|-------------|
| 触发方式 | 手动 | 自动（+手动） |
| Gene 来源 | 预设 | 自动检测生成 + 预设 |
| Skill | 手动维护 | 自动创建 + 手动维护 |
| 安全机制 | 基础 | ADL 全套保护 |
| 向量记忆 | 无 | qwen3-embedding:0.6b |
| 调度器 | PCEC Scheduler | 自适应心跳 |

> 💡 **建议**：两个系统配合使用 — XM-AUTO-EVO 负责日常观察学习，XM-EVO 负责定期人工 review 和精细化调整。

## 依赖

- Node.js >= 18.0.0
- CoPaw (openclaw-cn)
- Git
- **Ollama** (用于向量记忆，http://localhost:11434)
- **qwen3-embedding:0.6b** 模型（Ollama）

```bash
# 安装 ollama 模型
ollama pull qwen3-embedding:0.6b
```
