# XMclaw 技能系统深度调研计划

## 目标
对 XMclaw 技能系统进行深度调研，产出一份有真实实现和论文支持的正式调研报告。

## 背景
XMclaw 已有技能系统调研（docs/audit/SKILL_SYSTEM_SOTA_RESEARCH_2026.md），但用户认为仍有"很大问题"，需要更深入的研究。

## 已有调研识别的关键问题
1. **技能表示**: SKILL.md 已消费，但残留 L3 脚本/allowed_tools 强制
2. **获取/作者**: 轨迹归纳已落地，但可能不够完善
3. **组合**: 缺复合技能（技能调技能）
4. **课程**: 缺自动课程
5. **共享/市场**: MCP 已接入，但残留 SSE/OAuth/热重载/Alita 式自动生成
6. **自主调用**: 语义召回已落地，但可能不够完善

## 研究阶段

### Stage 1 — 深度研究（并行）
- **研究员_技能表示**: 调研 SKILL.md 标准、Voyager、ADAS、Alita 的最新进展，真实源码
- **研究员_评估进化**: 调研 HonestGrader、GEPA、DSPy、最新 self-evolving agents survey
- **研究员_组合课程**: 调研 Voyager 组合、Meta-Agent、自动课程最新进展
- **研究员_共享市场**: 调研 MCP、Anthropic Skills、技能市场最新进展
- **研究员_自主调用**: 调研 RAG-of-tools、ToolLLM、语义路由最新进展

### Stage 2 — 综合分析
- 整合所有研究发现
- 对比 XMclaw 现状与头部实现
- 给出具体差距和实现建议

### Stage 3 — 报告撰写
- 撰写正式调研报告（Markdown）
- 转换为 .docx 格式

## 输出
- `XMclaw_Skill_System_Deep_Research_2026.md`
- `XMclaw_Skill_System_Deep_Research_2026.docx`
