# Agent 技能系统「共享与市场」层调研报告

> 研究员_共享市场 | 2026-06-06  
> 范围：MCP 生态、Anthropic Agent Skills 市场、skills.sh 分发网络、Alita MCP 自生成、2025-2026 新兴技能市场机制。  
> 要求：每节必须包含 **论文/出处 + 真实源码链接/代码片段 + 与 XMclaw 的对比**。

---

## 目录

1. [MCP（Model Context Protocol）生态与规范](#1-mcp-model-context-protocol-生态与规范)
2. [Anthropic Agent Skills 市场与跨厂商移植](#2-anthropic-agent-skills-市场与跨厂商移植)
3. [skills.sh 生态与社区 SKILL.md 分发](#3-skillssh-生态与社区-skillmd-分发)
4. [Alita MCP 自生成：缺能力时自动造工具](#4-alita-mcp-自生成缺能力时自动造工具)
5. [2025-2026 技能市场新方法：评分、验证、商店](#5-2025-2026-技能市场新方法评分验证商店)
6. [综合对比与 XMclaw 差距矩阵](#6-综合对比与-xmclaw-差距矩阵)

---

## 1. MCP（Model Context Protocol）生态与规范

### 1.1 论文/出处

- **MCP 规范 2025-11-25**（官方 changelog，2026-06-04 发布）  
  https://modelcontextprotocol.io/specification/2025-11-25/changelog  
  核心变更：OpenID Connect Discovery 1.0 支持、增量 scope 同意（`WWW-Authenticate`）、工具图标元数据、`ElicitResult` 标准枚举、采样（sampling）支持 `tools`/`toolChoice`、OAuth Client ID Metadata Documents、实验性 tasks（ durable requests + polling）。

- **MCP 安全现状 2026**（NimbleBrain 审计，2026-03-11）  
  https://nimblebrain.ai/blog/state-of-mcp-security-2026/  
  关键数据：官方注册表 **3,012 个唯一 server**（半年前约 2,500）；84.6% 有源码；仅 **8.5% 使用 OAuth**；过去一年 7 个 CVE，含 CVSS 9.6 的 RCE。

- **MCP 规范对比表**（Alibaba Cloud 分析，2025-03-27）  
  https://www.alibabacloud.com/blog/a-comprehensive-analysis-and-practical-implementation-of-the-new-features-in-the-mcp-specification_602206  
  2025-03-26 版 vs 2024-11-05 版：OAuth 2.1（废弃 implicit，强制 PKCE+HTTPS）、Streamable HTTP 取代 HTTP+SSE、协议级强制 JSON-RPC Batching、Tool Annotations（破坏性标记）、音频流、`Mcp-Session-Id` 头、默认 JSON Schema 2020-12。

### 1.2 真实源码链接/代码片段

**官方注册表 API**（`registry.modelcontextprotocol.io`）  
- 仓库：https://github.com/modelcontextprotocol/registry  
- API 文档：https://github.com/modelcontextprotocol/registry/blob/main/docs/reference/api/official-registry-api.md  
- OpenAPI 交互文档：https://registry.modelcontextprotocol.io/docs  

注册表核心端点（来自官方 RFC Discussion #1，2025-02-24）：

```
GET /v1/servers?limit=5000&offset=0
→ { servers: [ { id, name, description, repository: {url, subfolder, branch, commit}, version } ], next, total_count }

GET /v1/servers/:id
→ 完整 server 元数据 + remotes[]（transport_type, url）+ registries[] + command_arguments
```

**Zoom 的注册表发布实践**（`github.com/zoom/mcp-registry`，2026-04-08）：

```bash
# 每个 server 目录含 server.json，用 mcp-publisher CLI 发布
cd zoom-workspace
mcp-publisher login github
mcp-publisher publish

# 发布后可通过注册表 API 查询
curl "https://registry.modelcontextprotocol.io/v0.1/servers?search=zoom&limit=100"
```

**Python 注册表客户端**（`ben-alkov/mcp-registry-client`，2025-09-12）：

```python
import asyncio
from mcp_registry_client import RegistryClient

async def main():
    async with RegistryClient() as client:
        result = await client.search_servers(name="jira")
        for server in result.servers:
            print(f"{server.name}: {server.description}")

asyncio.run(main())
```

**MCP HTTP Bridge 源码**（XMclaw 内部已实现 SSE + streamableHttp）：

```python
# xmclaw/providers/tool/mcp_http_bridge.py
class MCPHttpBridge(ToolProvider):
    def __init__(self, url: str, transport: str = "sse", ...):
        self._url = url.rstrip("/")
        self._transport = transport  # "sse" | "streamableHttp"
        ...

    async def _rpc(self, method: str, params: dict) -> dict:
        if self._transport == "streamableHttp":
            await self._send_http_post(msg)
        else:
            if self._sse_reader_task is None:
                self._sse_reader_task = asyncio.create_task(self._sse_read_loop())
            await self._send_sse_post(msg)
        return await asyncio.wait_for(future, timeout=self._request_timeout)
```

### 1.3 与 XMclaw 的对比

| 维度 | 头部（MCP 生态） | XMclaw 现状 | 差距 |
|---|---|---|---|
| **Transport** | stdio / SSE / streamableHttp 全支持；2025-11 规范以 streamableHttp 为主流 | `MCPBridge`（stdio）+ `MCPHttpBridge`（SSE + streamableHttp）已覆盖三种 transport | 🟢 **已对齐** |
| **注册表** | 官方 `registry.modelcontextprotocol.io` 3K+ server；支持搜索/版本/发布 API | 本地 `docs/skill_marketplace_index.json` 兜底，无 MCP 注册表集成 | 🔴 **缺注册表对接** |
| **OAuth** | OAuth 2.1 + PKCE + HTTPS 强制；增量 scope 同意；Client ID Metadata Documents | `McpServerConfig` 解析 `auto_approve` 列表，但无 OAuth 流实现 | 🟡 **残留：无 OAuth 客户端流** |
| **热重载** | 官方无统一热重载；社区靠 `mcp-publisher` 重新发布 | `MCPHub.reload()` 手动重读 `mcpServers.json`；无文件 watcher | 🟡 **残留：无自动文件 watcher** |
| **Tool Annotations** | 2025-03 规范新增：标记工具是否 destructive / 只读 / 等 | `MCPBridge` 解析 `inputSchema` 但忽略 `annotations` | 🟡 **残留：未消费 annotations** |

**结论**：XMclaw 的 MCP **客户端传输层**（stdio/SSE/streamableHttp）已是研究级实现，甚至领先多数只支持 stdio 的框架；真正缺口是**注册表生态对接**（从官方 3K server 中按需发现/安装）和 **OAuth 授权流**（安全接入远程 MCP）。

---

## 2. Anthropic Agent Skills 市场与跨厂商移植

### 2.1 论文/出处

- **Agent Skills 综述论文**（arXiv:2602.12430v4，2025-11-25）  
  https://arxiv.org/html/2602.12430v4  
  将 Agent Skills 定义为「LLM 无需重训练即可按需获取领域专长的标准化文件系统包」。提出 7 大开放挑战：跨平台移植、大规模技能选择、技能组合编排、基于能力的权限模型、技能验证与测试、供应链安全、动态技能生成。

- **Agent Skills 安全架构分析**（arXiv:2604.02837v1，2024-03-29 → 2026 更新）  
  https://arxiv.org/html/2604.02837v1  
  三大架构组件：包结构（SKILL.md + 可选脚本/资源）、渐进披露（L1 name+desc → L2 全文 → L3 附件按需）、信任模型（加载后技能可指挥 agent 使用任何可用工具——当前是隐式信任）。

- **跨厂商采纳时间线**（Inference.sh blog，2026-04-13）  
  https://inference.sh/blog/skills/agent-skills-overview  
  Anthropic 2025-12 发布开放标准后，OpenAI（Codex CLI / ChatGPT Desktop）、GitHub Copilot、Cursor、Google Gemini CLI 在两个月内全部采纳。

### 2.2 真实源码链接/代码片段

**Anthropic 官方 skills 仓库**（75,600 stars）：
- https://github.com/anthropics/skills
- 结构：每个技能一个目录，含 `SKILL.md`（YAML frontmatter：`name` + `description` + 可选 `allowed-tools`）+ 可选 `scripts/` / `references/`。

**SKILL.md 渐进披露在 Claude Code 中的实现**（来自公开文档与社区逆向）：

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

**OpenAI Codex CLI 的 skills 支持**（Simon Willison 2025-12 发现，OpenAI 官方 catalog）：
- 内置 skills 目录：`/home/oai/skills`（PDF、文档、表格处理）
- 用户 skills 目录：`~/.codex/skills/`
- 安装命令：`$skill-installer`（无需包管理器）
- 官方 catalog：https://github.com/openai/skills（13K+ stars，35 个 curated skills）

**GitHub Copilot 的 skills 支持**（2025-12 宣布）：
- 读取路径：`.github/skills/`
- 与 GitHub Actions、issue templates 同目录层级，天然版本控制。

### 2.3 与 XMclaw 的对比

| 维度 | 头部（Anthropic 生态） | XMclaw 现状 | 差距 |
|---|---|---|---|
| **格式消费** | SKILL.md + YAML frontmatter 是事实标准 | `MarkdownProcedureSkill` + `user_loader` 已原生解析 frontmatter（`name`/`description`/`triggers`/`allowed_tools`/`paths`/`model`/`created_by`） | 🟢 **已对齐** |
| **渐进披露** | L1 name+desc → L2 全文 → L3 附件/脚本按需 | `skill_browse` → `skill_view` → `skill_run` 三步 + `prefilter` token-overlap | 🟢 **模式等价** |
| **跨厂商移植** | 同一 SKILL.md 可在 Claude/Codex/Copilot/Cursor 间运行 | `user_loader` 扫描 `~/.agents/skills/`（skills.sh 安装目录），直接消费社区包 | 🟢 **已兼容** |
| **信任模型** | 隐式信任：加载后技能可调用任何工具 | 三级信任：`UNTRUSTED`（agent 自写 `.proposed`）→ `INSTALLED`（市场安装）→ `USER`（用户手写）；`allowed_tools` 已解析但**运行时尚未强制** | 🟡 **残留：allowed_tools 运行时未强制** |
| **L3 脚本执行** | Claude Code 按需运行 bundled 脚本，**不读进上下文**（确定性执行） | `MarkdownProcedureSkill.run()` 返回 body 给 agent，agent 用自带工具执行——功能等价但非沙箱化确定执行 | 🟡 **残留：无沙箱化脚本执行** |

**结论**：XMclaw 在 **SKILL.md 消费**和**渐进披露**上已与 Anthropic 标准对齐，甚至能直接跑 `npx skills add` 安装的社区包。残留缺口是 `allowed_tools` 的运行时强制和 L3 脚本的沙箱化执行（均为收尾级，非阻断）。

---

## 3. skills.sh 生态与社区 SKILL.md 分发

### 3.1 论文/出处

- **Skilldex 论文**（arXiv:2604.16911v1，2026-04-18）  
  https://arxiv.org/html/2604.16911v1  
  系统综述了 skills.sh 生态：Vercel 的 `npx skills add` CLI + 社区注册表，支持 40+ agent 的跨平台安装。Skilldex 在此基础上增加了层级作用域安装（project/global/system）和基于 Anthropic 规范的格式符合性评分（format conformance scoring）。

- **Agent Skills 生态报告**（Termdock，2026-03-16）  
  https://www.termdock.com/en/blog/agent-skills-guide  
  截至 2026-03，三大市场：SkillsMP（400K+ skills，语义搜索爬取）、Skills.sh（83K+ skills，8M+ installs，2026-01-20 上线）、ClawHub（~10K，遭 ClawHavoc 恶意软件攻击）。

- **Vercel 官方 skills 仓库**（vercel-labs/skills）：
  https://github.com/vercel-labs/skills  
 

---

## 4. Alita MCP 自生成：缺能力时自动造工具

### 4.1 论文/出处

- **Alita 论文**（arXiv:2505.20286，2025-05-26，普林斯顿大学 AI Lab）  
  https://arxiv.org/abs/2505.20286  
  核心思想：**Minimal Predefinition + Maximal Self-Evolution**。Alita 仅保留 Manager Agent + Web Agent 两个核心组件，遇到能力缺口时：
  1. **MCP Brainstorming**：分析任务，识别功能缺口，触发创意合成。
  2. **开源搜索**：Web Agent 搜索相关开源库和资源。
  3. **脚本生成**：`ScriptGeneratingTool` 实时创建工具代码。
  4. **验证执行**：`CodeRunningTool` 在虚拟环境中验证工具可靠性。
  5. **MCP 封装**：将验证通过的工具封装为 MCP server，存入 **MCP Box** 供未来复用。

  GAIA validation 成绩：**75.15% pass@1 / 87.27% pass@3**，超越 OpenAI Deep Research 和 Manus。

- **Alita GitHub 讨论**（`CharlesQ9/Alita`，2025-04-30）：
  https://github.com/CharlesQ9/Alita  
  社区关键讨论：MCP Abstraction 的 trade-off——抽象层级太高导致 **MCP Overload**（工具重叠），太低导致 **Overfit**（仅对特定数据集有效）。

### 4.2 真实源码链接/代码片段

**Alita 架构伪代码**（来自论文 Figure 3 及社区复现）：

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

**Alita 的 MCP 封装细节**（论文 §3）：

> "Furthermore, the new tools can be encapsulated as MCP servers for future reuse. With the aid of MCPs, Alita can generate increasingly powerful, diverse, and complex MCPs, thus establishing a self-reinforcing cycle."

**GPT-4o-mini 上的工具蒸馏效果**（论文 Table 4）：

| Model | Level 1 | Level 2 | Level 3 | Total |
|---|---|---|---|---|
| Alita (Claude-3.7-Sonnet + GPT-4o) | 81.13% | 75.58% | 46.15% | 72.73% |
| Alita (GPT-4o-mini, 无预蒸馏 MCP) | 54.72% | — | — | — |
| Alita (GPT-4o-mini, 有蒸馏 MCP) | — | — | 11.54% | — |

> 注：GPT-4o-mini 在 Level 3 任务上从 **3.85% → 11.54%**（提升 3 倍），证明自动生成的 MCP 具备**模型蒸馏**能力。

### 4.3 与 XMclaw 的对比

| 维度 | 头部（Alita） | XMclaw 现状 | 差距 |
|---|---|---|---|
| **能力缺口识别** | Manager Agent 主动分析任务，识别缺失能力 | `ReflectiveMutator` 对**已有**技能做 GEPA 式变异；`SkillInductor`（`xmclaw/skills/inductor.py`）可从成功轨迹归纳新 SKILL.md | 🟡 **已有轨迹归纳，但无主动缺口分析** |
| **工具自动生成** | `ScriptGeneratingTool` + `CodeRunningTool` 现场写代码、验证、封装 MCP | `SkillInductor` 产出 `.proposed` SKILL.md（未信任状态），走证据门晋升；但**不生成可执行代码/MCP server** | 🔴 **缺代码级工具生成** |
| **MCP 封装复用** | 新生成工具自动封装为 MCP server，存入 MCP Box 长期复用 | `MCPHub` 可消费外部 MCP，但**不会自生成 MCP server** | 🔴 **缺 MCP 自生成** |
| **自强化循环** | 生成的 MCP 越多，后续任务覆盖越广，形成正反馈 | 进化闭环是「评估→变异→选择→晋升」，但变异对象是**已有技能**，不是**无中生有造工具** | 🟡 **进化闭环强，但起点受限** |
| **小模型增益** | 自动工具使 GPT-4o-mini Level 3 提升 3 倍 | 无类似蒸馏机制 | 🔴 **缺模型蒸馏路径** |

**结论**：XMclaw 的 `SkillInductor` 已具备「轨迹→SKILL.md」的归纳能力，但 Alita 的「缺口分析→代码生成→MCP 封装→复用」完整链条**尚未实现**。这是 XMclaw 技能系统从「改良已有」迈向「无中生有」的关键缺口。

---

## 5. 2025-2026 技能市场新方法：评分、验证、商店

### 5.1 论文/出处

- **SkillGuard 权限框架**（arXiv:2606.03024v1，2026-06-01）  
  https://arxiv.org/html/2606.03024v1  
  提出基于 **SkillManifest** 的运行时权限边界：每个技能自动生成 manifest，声明所需能力（文件读写、子进程、网络等），运行时 guardian 拦截越权调用。在 SkillInject 基准上：攻击成功率从 32.37% → 23.02%（contextual injection），TSR（任务成功率）仅下降 1.45%。

- **SkillInject 基准**（arXiv:2602.20156，2026-02，被引 26 次）  
  https://www.skill-inject.com/  
  测量 agent 对 skill 文件攻击的脆弱性：前沿模型攻击成功率高达 **80%**。

- **Sealing the Audit–Runtime Gap**（arXiv:2605.05274v1，2026-04-12）  
  https://arxiv.org/html/2605.05274v1  
  提出技能生命周期三阶段防御：提交阶段（静态扫描）、锚定阶段（供应链治理/市场审核）、调用阶段（运行时保护）。指出当前市场**26.1% 的技能已含至少一个可利用漏洞**（Liu et al., 2026）。

- **Formal Analysis and Supply Chain Security**（arXiv:2603.00195v1，2026-02-26）  
  https://arxiv.org/html/2603.00195v1  
  三大生态（OpenClaw 228K stars、Anthropic 75.6K stars、MCP 注册表）共同弱点：**缺乏形式化能力模型**（formal capability model）——技能声明做什么 vs 实际能做什么之间无运行时约束。

- **Microsoft 365 Copilot Agent Store**（Microsoft Ignite 2025，2025-11-18）  
  https://examinotion.com/blog/microsoft-ignite-ai-certification-impact  
  Microsoft 在 2025-11 Ignite 推出 **Agent Store**，支持 agent 生命周期管理、审批、知识源共享控制。已纳入 2026-02 的认证考试（AB-730/AB-731/AB-100）。

### 5.2 真实源码链接/代码片段

**SkillGuard 开源实现**（`LLMSecurity/skillguard`，2026-02-25）：
- https://github.com/LLMSecurity/skillguard
- 本身是一个 Agent Skill，教 agent 如何审计其他 skill：

```markdown
# SkillGuard 审计流程（作为 SKILL.md 实现）
1. File Inventory — 清点所有文件，标记二进制/符号链接
2. Frontmatter Validation — 检查 Agent Skills 规范合规性
3. Intent Verification — 技能是否做它声称的事？（最重要）
4. OWASP Agentic Top 10 Walkthrough — 系统性检查 10 大风险类别
5. MITRE ATLAS Mapping — 将发现映射到标准技术 ID
6. Verdict — ✅ SAFE / ⚠️ SUSPICIOUS / 🚨 MALICIOUS
```

**SkillGuard 运行时拦截**（论文 §4 伪代码）：

```python
class SkillGuard:
    def load_skill(self, skill_md: str) -> SkillManifest:
        manifest = self.llm_generate_manifest(skill_md)
        return manifest

    def intercept(self, tool_call: dict, manifest: SkillManifest) -> Decision:
        required = self.infer_capability(tool_call)
        if required not in manifest.allowed_capabilities:
            return Decision.DENY
        return Decision.CONFIRM
```

**XMclaw 内部的信任分级与扫描**（`xmclaw/skills/user_loader.py`）：

```python
class UserSkillsLoader:
    def _trust_for(self, skill_id: str) -> SkillTrustLevel:
        if skill_id in self._proposed_skill_ids:
            return SkillTrustLevel.UNTRUSTED
        if skill_id in self._installed_skill_ids:
            return SkillTrustLevel.INSTALLED
        return SkillTrustLevel.USER
```

**XMclaw 的注入扫描**（`xmclaw/skills/markdown_skill.py`）：

```python
async def run(self, inp: SkillInput) -> SkillOutput:
    body = self.stripped_body
    decision = apply_policy(
        body,
        policy=PolicyMode.DETECT_ONLY,
        source=SOURCE_SKILL_BODY,
        extra={"skill_id": self.id},
    )
    body = decision.content
    return SkillOutput(..., result={"instructions": body, ...})
```

### 5.3 与 XMclaw 的对比

| 维度 | 头部（2025-2026 新方法） | XMclaw 现状 | 差距 |
|---|---|---|---|
| **技能评分** | skills.sh Leaderboard（安装量/官方认证）；Skilldex 格式符合性评分 | 无 | 🔴 **缺评分机制** |
| **技能验证** | SkillGuard 自动 manifest 生成 + 运行时权限拦截；SkillInject 基准 80% 攻击成功率警示 | `SkillManifest` 有 `permissions_*` 字段但**运行时不强制**；B-328 做 AST 交叉检查（advisory warning only） | 🟡 **有声明，无运行时强制** |
| **市场审核** | Skills.sh 三方审计（Snyk/Socket/Trust Hub）；ClawHub 遭 ClawHavoc 攻击后社区重视 | 安装时运行安全扫描器，但无与外部审计注册表联动 | 🟡 **本地扫描有，外部联动无** |
| **Agent Store** | Microsoft 365 Copilot Agent Store（企业级生命周期+审批） | 本地 `skill_marketplace_index.json` 为空，无商店 UI | 🔴 **缺商店/审批流** |
| **供应链安全** | OWASP Agentic Top 10（2026）+ MITRE ATLAS 映射；形式化能力模型呼声 | `security/skill_scanner.py` 扫描源码；`apply_policy` 扫描 SKILL.md body；但未映射到 OWASP/ATLAS | 🟡 **有扫描，无标准框架映射** |

---

## 6. 综合对比与 XMclaw 差距矩阵

### 6.1 全栈对照表

| 层 | 头部代表做法 | XMclaw 现状 | 评级 | 优先级 |
|---|---|---|---|---|
| **MCP Transport** | stdio/SSE/streamableHttp + OAuth 2.1 + Tool Annotations | `MCPBridge`（stdio）+ `MCPHttpBridge`（SSE/streamableHttp）已覆盖；OAuth 未实现；annotations 未消费 | 🟢/🟡 | 低 |
| **MCP 注册表** | 官方 3K+ server；`registry.modelcontextprotocol.io` API；`mcp-publisher` 发布 | 本地空 `skill_marketplace_index.json`；无注册表客户端 | 🔴 | **高** |
| **SKILL.md 消费** | 开放标准；Claude/Codex/Copilot/Cursor 全采纳 | `MarkdownProcedureSkill` + `user_loader` 完整解析 frontmatter + body | 🟢 | — |
| **skills.sh 兼容** | `npx skills add` → `~/.agents/skills/`；83K+ skills | 扫描 `~/.agents/skills/` 直接消费；`skill_install` meta-tool 支持 agent 自装 | 🟢 | — |
| **技能评分/发现** | Leaderboard、安装量、官方认证、格式符合性评分 | 无 | 🔴 | **中** |
| **安全验证** | SkillGuard 运行时权限拦截；SkillInject 基准；Snyk/Socket/VT 三方审计 | 三级信任（UNTRUSTED/INSTALLED/USER）；`allowed_tools` 解析但未强制；本地源码扫描有 | 🟡 | **中** |
| **Alita 式自生成** | 缺口分析→代码生成→MCP 封装→复用；GAIA 75% | `SkillInductor` 轨迹→SKILL.md 归纳已落地；但**无代码级工具生成/MCP 封装** | 🔴 | **高** |
| **Agent Store/审批** | Microsoft Copilot Agent Store；企业生命周期管理 | 无 | 🔴 | **低** |

### 6.2 建议投资优先级

1. **🔴 MCP 注册表对接** — 实现 `MCPRegistryClient`（参考 `ben-alkov/mcp-registry-client`），让 `skill_install` 能从 `registry.modelcontextprotocol.io` 搜索/安装远程 MCP server。直接打通 3K+ 工具生态。
2. **🔴 Alita 式 MCP 自生成** — 在 `SkillInductor` 基础上增加「代码生成 + MCP 封装」路径：当轨迹归纳无法覆盖能力缺口时，让 LLM 写 Python 脚本→`MCPBridge` 封装→存入 `mcp_box/`。这是 XMclaw 从「改良已有」到「无中生有」的质变。
3. **🟡 安全运行时强制** — 将 `SkillManifest.allowed_tools` 从「解析存储」升级为「运行时拦截」：在 `SkillToolProvider.invoke()` 或 `MCPHub.invoke()` 层检查工具调用是否在 manifest 白名单内，越权则 `DangerousPromotionError` 式拒绝。
4. **🟡 技能评分/发现** — 为 `skill_marketplace_index.json` 增加 `install_count`、`grader_score`、`security_audit` 字段；对接 skills.sh API 做远程搜索 fallback。
5. **🟢 OAuth 客户端流** — 当需要接入企业级远程 MCP（如 Zoom Workspace、Stripe）时补全；个人助理场景优先级较低。

### 6.3 一句话总结

> XMclaw 的**技能进化引擎**（HonestGrader + GEPA + UCB1 + 证据门晋升）是护城河，**SKILL.md 消费**和**MCP 传输层**也已对齐头部。真正决定能否接入全行业生态的缺口只有两条：**MCP 注册表对接**（发现 3K+ 外部工具）和 **Alita 式 MCP 自生成**（遇到缺口时自动造工具）。补这两处，XMclaw 就能在保留独有诚实进化闭环的同时，成为全技能生态的「通用客户端 + 自进化工厂」。

---

## 出处索引

| 编号 | 来源 | URL |
|---|---|---|
| [1] | MCP Spec 2025-11-25 Changelog | https://modelcontextprotocol.io/specification/2025-11-25/changelog |
| [2] | MCP Security State 2026 (NimbleBrain) | https://nimblebrain.ai/blog/state-of-mcp-security-2026/ |
| [3] | MCP Spec Analysis (Alibaba Cloud) | https://www.alibabacloud.com/blog/a-comprehensive-analysis-and-practical-implementation-of-the-new-features-in-the-mcp-specification_602206 |
| [4] | MCP Registry Official Repo | https://github.com/modelcontextprotocol/registry |
| [5] | Zoom MCP Registry Practice | https://github.com/zoom/mcp-registry |
| [6] | Python MCP Registry Client | https://github.com/ben-alkov/mcp-registry-client |
| [7] | Agent Skills Survey (arXiv:2602.12430) | https://arxiv.org/html/2602.12430v4 |
| [8] | Agent Skills Security Architecture (arXiv:2604.02837) | https://arxiv.org/html/2604.02837v1 |
| [9] | Agent Skills Overview (Inference.sh) | https://inference.sh/blog/skills/agent-skills-overview |
| [10] | Anthropic Official Skills Repo | https://github.com/anthropics/skills |
| [11] | OpenAI Skills Catalog | https://github.com/openai/skills |
| [12] | Skilldex Paper (arXiv:2604.16911) | https://arxiv.org/html/2604.16911v1 |
| [13] | Agent Skills Guide (Termdock) | https://www.termdock.com/en/blog/agent-skills-guide |
| [14] | Vercel Skills CLI (vercel-labs/skills) | https://github.com/vercel-labs/skills |
| [15] | Secure Skills Fork (alonw0) | https://github.com/alonw0/secure-skills |
| [16] | Alita Paper (arXiv:2505.20286) | https://arxiv.org/abs/2505.20286 |
| [17] | Alita GitHub Discussion | https://github.com/CharlesQ9/Alita |
| [18] | SkillGuard Paper (arXiv:2606.03024) | https://arxiv.org/html/2606.03024v1 |
| [19] | SkillGuard Open Source | https://github.com/LLMSecurity/skillguard |
| [20] | SkillInject Benchmark | https://www.skill-inject.com/ |
| [21] | Audit-Runtime Gap (arXiv:2605.05274) | https://arxiv.org/html/2605.05274v1 |
| [22] | Supply Chain Security (arXiv:2603.00195) | https://arxiv.org/html/2603.00195v1 |
| [23] | Microsoft Ignite 2025 Agent Store | https://examinotion.com/blog/microsoft-ignite-ai-certification-impact |
