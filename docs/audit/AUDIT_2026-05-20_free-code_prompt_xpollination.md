# Cross-Audit: free-code-main → XMclaw Prompt Engineering

**Date:** 2026-05-20  
**Auditor:** Kimi Code CLI (manual deep-dive)  
**Scope:** `free-code-main/src/{tools/**/prompt.ts, constants/prompts.ts, services/{compact,extractMemories,SessionMemory,MagicDocs}/prompts.ts}`  
**XMclaw Baseline:** Commit `4d7d191` (Phase 1 Chinese-native prompt overhaul)

---

## 1. Executive Summary

free-code-main (Claude Code) has spent ~2 years iterating on prompt engineering at production scale (10K+ daily sessions). Its prompt architecture is **layered, cache-aware, and heavily templated**. XMclaw's Phase 1 Chinese-native overhaul is already excellent in honesty rules, edge-case discipline, and plan-first reporting. The gap lies in **structural patterns** that free-code has evolved for cache efficiency, tool-selection clarity, and output token optimization.

This audit identifies **10 patterns worth porting**, ranked by impact × effort.

---

## 2. free-code's Core Prompt Architecture

### 2.1 Static/Dynamic System Prompt Boundary

```typescript
// src/constants/prompts.ts:114
export const SYSTEM_PROMPT_DYNAMIC_BOUNDARY =
  '__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__'
```

The system prompt is split into two zones:

| Zone | Function | Cache scope |
|------|----------|-------------|
| **Static prefix** | Identity, capability list, discipline rules, tool-preference hierarchy | `global` — shared across all sessions in the org |
| **Dynamic suffix** | Session-specific guidance, memory, env info, language, MCP instructions, scratchpad | Per-session — re-computed each turn |

Key abstraction:
```typescript
// src/constants/systemPromptSections.ts
function systemPromptSection(name, compute)          // memoized — compute once, cache
function DANGEROUS_uncachedSystemPromptSection(name, compute, reason) // recompute every turn
```

**Why this matters:** Anthropic's prompt cache shares the prefix across turns. Every dynamic bit that leaks into the static prefix fragments the cache key, burning ~20K tokens/turn (per free-code's own metrics in PR #24490). XMclaw currently rebuilds the entire prompt each turn via `_with_fresh_time()` — the timestamp block is appended but the whole prompt is re-assembled.

**Portability:** Medium — requires refactoring `prompt_builder.py` to separate cacheable vs. per-turn sections.

### 2.2 Tool Prompts: Function-per-file + Dynamic Assembly

Every tool has its own `prompt.ts` with a `getPrompt()` function that assembles the description from runtime state (feature flags, env vars, sandbox config, subscription tier).

Example: `BashTool/prompt.ts` (369 lines) builds:
- Tool preference hierarchy ("Use Read instead of cat, use Edit instead of sed")
- Sandbox restrictions (deduped, normalized)
- Git safety protocol (NEVER skip hooks, NEVER amend without explicit request)
- Background task guidance (`run_in_background` parameter)
- Sleep discipline ("Do not sleep between commands that can run immediately")

**XMclaw gap:** `_specs.py` descriptions are static strings. They describe *what* the tool does, but not *when to prefer it over bash* or *when NOT to use it*.

### 2.3 "When to Use / When NOT to Use" Binary Guidance

`TaskCreateTool/prompt.ts` is the canonical example:

```
## When to Use This Tool
- Complex multi-step tasks (3+ distinct steps)
- Non-trivial and complex tasks
- Plan mode
- User explicitly requests todo list
- ...

## When NOT to Use This Tool
- Single, straightforward task
- Trivial and tracking provides no benefit
- Less than 3 trivial steps
- Purely conversational or informational
```

This binary framing eliminates ambiguity. The LLM sees both sides and picks correctly.

**XMclaw gap:** None of the 30+ tool specs in `_specs.py` have "When NOT to use" guidance. The agent frequently over-uses `remember` for transient context, `todo_write` for 1-step tasks, and `memory_search` before `file_read`.

### 2.4 XML Tag Structured Content

`src/constants/xml.ts` defines 30+ XML tag constants:

```typescript
export const BASH_INPUT_TAG = 'bash-input'
export const BASH_STDOUT_TAG = 'bash-stdout'
export const TASK_NOTIFICATION_TAG = 'task-notification'
export const FORK_BOILERPLATE_TAG = 'fork-boilerplate'
export const TICK_TAG = 'tick'
```

These wrap content so the LLM understands semantic type without parsing prose. Example: a background task completion arrives as:

```xml
<task-notification>
  <task-id>agent-abc</task-id>
  <status>completed</status>
  <summary>...</summary>
</task-notification>
```

**XMclaw gap:** Tool results and compaction summaries are plain text / markdown. No semantic tagging.

### 2.5 Compaction Prompt Template

`services/compact/prompt.ts` is a masterpiece of summarization engineering:

- **NO_TOOLS_PREAMBLE**: Aggressive upfront "Respond with TEXT ONLY. Do NOT call any tools. Tool calls will be REJECTED."
- **`<analysis>` drafting block**: The LLM writes a scratchpad analysis first (chronological review of each message, decisions, errors, user feedback). This block is **stripped** before the summary reaches context — it's a reasoning scaffold.
- **`<summary>` output block**: 9 structured sections (Primary Request, Key Concepts, Files & Code, Errors & Fixes, Problem Solving, All User Messages, Pending Tasks, Current Work, Optional Next Step)
- **Three variants**: BASE (full conversation), PARTIAL (recent messages only), UP_TO (prefix summary for continuing sessions)
- **Custom instructions injection**: Users can add per-project compact instructions

**XMclaw gap:** XMclaw relies on platform-level context compaction (Kimi's built-in). No custom summarization prompt.

### 2.6 Output Efficiency / Communication Discipline

For ant users, free-code has a **length-anchored** output efficiency section:

```
Length limits: keep text between tool calls to ≤25 words.
Keep final responses to ≤100 words unless the task requires more detail.
```

Research showed ~1.2% output token reduction vs. qualitative "be concise" (PR reference in source). The section also covers:
- Flowing prose, no fragments
- Inverted pyramid (lead with action)
- Match response to task complexity
- No filler, no stating the obvious
- No superlatives to oversell small wins

**XMclaw gap:** Narration discipline (B-206) exists but is qualitative. No numeric anchors.

### 2.7 Code Style Sub-discipline

free-code's `getSimpleDoingTasksSection()` includes a `codeStyleSubitems` block:

```
- Don't add features, refactor code, or make "improvements" beyond what was asked.
- Don't add error handling for scenarios that can't happen.
- Don't create helpers for one-time operations.
- Default to writing no comments. Only add one when the WHY is non-obvious.
- Don't explain WHAT the code does — well-named identifiers already do that.
- Before reporting a task complete, verify it actually works.
```

**XMclaw gap:** No code style guidance in the system prompt. The agent occasionally over-comments, over-engineers, or reports completion without verification.

### 2.8 Memory Extraction Agent Prompt

`services/extractMemories/prompts.ts` runs a **perfect fork** of the main conversation to extract memories in the background:

- Two-phase save: (1) write memory file, (2) update MEMORY.md index
- Four-type taxonomy (auto-only or combined with team memory)
- Budget management: "You have a limited turn budget. Turn 1 — read all files in parallel. Turn 2 — write all edits in parallel."
- Strict scoping: "Do NOT waste turns grepping source files or reading code to confirm patterns"

**XMclaw gap:** `llm_extractors.py` has concise Chinese prompts but lacks the two-phase save guidance, turn budgeting, and strict scoping of the extraction agent.

### 2.9 Session Memory Template

`services/SessionMemory/prompts.ts` defines a structured note-taking template:

```markdown
# Session Title
# Current State
# Task specification
# Files and Functions
# Workflow
# Errors & Corrections
# Codebase and System Documentation
# Learnings
# Key results
# Worklog
```

With **strict structural preservation rules**:
- NEVER modify section headers
- NEVER modify italic description lines (template instructions)
- ONLY update content below the descriptions
- Keep each section under ~2000 tokens
- Always update "Current State" for continuity after compaction

**XMclaw gap:** No session memory template or structured note-taking prompt.

### 2.10 MagicDocs Philosophy

`services/MagicDocs/prompts.ts` encodes a documentation philosophy:

```
DOCUMENTATION PHILOSOPHY:
- BE TERSE. High signal only.
- Documentation is for OVERVIEWS, ARCHITECTURE, and ENTRY POINTS
- Do NOT duplicate information obvious from reading source code
- Focus on: WHY things exist, HOW components connect, WHERE to start reading
- Skip: detailed implementation steps, exhaustive API docs, play-by-play narratives
```

**XMclaw gap:** No auto-documentation system or documentation philosophy prompt.

---

## 3. Gap Analysis: XMclaw vs. free-code

| Dimension | free-code | XMclaw (post-Phase-1) | Gap |
|-----------|-----------|----------------------|-----|
| **System prompt architecture** | Static/dynamic boundary, cache-scoped sections | Single monolithic string, rebuilt each turn | Large — missing cache optimization |
| **Tool descriptions** | Dynamic `getPrompt()` per tool, with when-to/when-not guidance | Static `ToolSpec.description` strings | Medium — missing scenario guidance |
| **Binary guidance** | "When to use / When NOT to use" for every tool | None | Medium — ambiguity in tool selection |
| **XML structured content** | 30+ semantic tags for all content types | Plain text / markdown only | Medium — no semantic parsing hints |
| **Compaction** | Custom 9-section summarization prompt with `<analysis>` scaffold | Platform-level compaction only | Medium — no custom summarization |
| **Output efficiency** | Numeric anchors (≤25 words between calls, ≤100 final) | Qualitative narration discipline (B-206) | Small — add numeric anchors |
| **Code style** | 6 explicit sub-rules (no comments by default, verify before reporting) | None | Small — add code style section |
| **Memory extraction** | Two-phase save, turn budget, strict scoping | Concise Chinese prompt, JSON-only | Medium — add extraction discipline |
| **Session memory** | 10-section template with structural preservation rules | No session memory system | Large — would require new subsystem |
| **Honesty rules** | General trustworthiness guidance | Extremely specific (B-302) with concrete chat IDs and正反例 | XMclaw **leads** here |
| **Edge case discipline** | General tool guidance | 7 explicit edge-case rules (empty, timeout, permission, drift, omission, single-word, repetition) | XMclaw **leads** here |
| **Plan-first discipline** | TodoWrite for tracking | Three-phase structure (PLAN → PROGRESS → SYNTHESIS) with approval gate (B-239) | XMclaw **leads** here |
| **Proactive mode** | Terminal focus awareness, sleep discipline | Time-of-day quiet hours, tick-loop | Comparable |
| **Skill dispatch** | SkillTool with BLOCKING REQUIREMENT | Skill-first dispatch (B-177) + skill_browse discovery (B-299) | Comparable |
| **Vision guidance** | Basic multimodal note | 3-layer priority + chat-app shortcut + batch atomicity | XMclaw **leads** here |
| **Self-modification** | Not applicable (cloud-hosted) | Active problem-solving loop, editable codebase | XMclaw **leads** here |

---

## 4. Recommendations: What to Port (Ranked)

### P0 — High Impact, Low Effort

#### 4.1 Add "When to Use / When NOT to Use" to high-ambiguity tools
**Target:** `_specs.py` — `memory_search`, `remember`, `learn_about_user`, `todo_write`, `canvas_create`, `schedule_followup`  
**Pattern:** Copy free-code's `TaskCreateTool` binary framing  
**Example for `remember`:**
```python
description=(
    "将跨会话持久事实写入 MEMORY.md。\n\n"
    "## 何时使用\n"
    "  • 用户明确要求'记住'某事\n"
    "  • 你做出了一个跨会话仍需的决策（项目约定、技术选型）\n"
    "  • 你发现了反复出现的约束或失败模式\n"
    "## 何时不使用\n"
    "  • 本会话临时上下文（用 todo_write）\n"
    "  • 用户个人信息（用 learn_about_user）\n"
    "  • 一次性观察，下回合就不再相关\n\n"
    "每次调用在匹配的 ## 分类下追加时间戳 bullet..."
)
```

#### 4.2 Add numeric output anchors to narration discipline
**Target:** `prompt_builder.py` B-206 section  
**Addition:**
```
量化约束（减少 token 浪费）:
  • 工具调用之间的文字 ≤30 个中文字符或 ≤25 个英文单词。
  • 最终回复 ≤120 个中文字符或 ≤100 个英文单词，除非任务本身需要详细展开。
  • 简单问题直接回答，不要用标题和编号分段。
```

#### 4.3 Add code style sub-discipline
**Target:** `prompt_builder.py`, new section after "Self-management toolkit"  
**Content (adapted from free-code, Chinese-native):**
```
代码风格纪律:
  • 默认不写注释。只在 WHY 非显而易见时写一条：隐藏约束、微妙不变量、特定 bug 的 workaround。
  • 不要解释 WHAT —— 命名良好的标识符已经做到了。
  • 不要给没改的代码加 docstring / type annotation / 注释。
  • 不要为一次性操作创建 helper / utility / 抽象。
  • 不要给不可能发生的场景加 error handling。只在系统边界（用户输入、外部 API）验证。
  • 报告完成前，验证它真的工作了：跑测试、执行脚本、检查输出。无法验证时明确说明，不要假装成功。
```

### P1 — Medium Impact, Medium Effort

#### 4.4 Refactor system prompt into cacheable vs. dynamic sections
**Target:** `prompt_builder.py`  
**Pattern:** Introduce a boundary marker and section functions:
```python
# Static — built once at import, never changes within a session
_STATIC_SECTIONS = [
    _build_identity_section(),      # OS, home, Desktop, shell hint
    _build_capability_section(),    # Available tools list
    _build_vision_section(),        # 3-layer priority
    _build_harder_rules_section(),  # B-208, B-302, B-217, B-239
    _build_skill_section(),         # Installation, dispatch, browse
    _build_self_management_section(), # memory_search, ask_user_question, etc.
    _build_notes_section(),         # note_write, journal_append
    _build_self_evolution_section(), # 7 persona files
    _build_narration_section(),     # B-206
    _build_edge_case_section(),     # ★ 自主调用与边缘场景纪律
    _build_code_style_section(),    # NEW: P0 #4.3
]

# Dynamic — rebuilt each turn
_DYNAMIC_SECTIONS = [
    _build_timestamp_section(),     # ## 当前时刻
    _build_focus_section(),         # update_focus injection
    _build_todo_section(),          # Current todo list
]
```
**Note:** This is architectural groundwork for future prompt caching. The immediate benefit is cleaner organization even without cache sharing.

#### 4.5 Add `<analysis>` / `<summary>` scaffold to compaction
**Target:** If/when XMclaw implements custom compaction (platform-level is currently used)  
**Pattern:** free-code's `formatCompactSummary()` strips `<analysis>` before injecting into context. The analysis block is a reasoning scaffold that improves summary quality but doesn't bloat context.  
**Deferred:** Platform compaction handles this for now.

### P2 — Lower Priority / Requires New Subsystems

#### 4.6 Session memory template + auto-update agent
**Target:** New subsystem (Epic #24 Phase 2.1 equivalent)  
**Effort:** High — needs template storage, extraction agent, update prompt, section size tracking, budget warnings.  
**Deferred:** Not critical for v1.1.0.

#### 4.7 MagicDocs auto-documentation
**Target:** New subsystem  
**Effort:** High — needs doc directory, update agent, custom instructions support.  
**Deferred:** Not critical for v1.1.0.

#### 4.8 XML tag constants for structured content
**Target:** New module `xmclaw/core/xml_tags.py`  
**Use cases:**
- `<bash-input>` / `<bash-stdout>` / `<bash-stderr>` for shell command wrapping
- `<task-notification>` for background task completions
- `<memory-context>` for injected memory snippets
- `<goal-anchor>` for update_focus injection
**Effort:** Medium — requires updating all content producers and consumers.  
**Deferred:** Architectural decision — plain text works well enough for now.

---

## 5. Concrete Implementation Plan for v1.1.0

### Commit 1: Tool binary guidance (P0 #4.1)
- Update `_specs.py`: add "When to use / When NOT to use" to `memory_search`, `remember`, `learn_about_user`, `todo_write`, `canvas_create`
- Update tests: `test_specs.py` if description assertions exist

### Commit 2: Numeric anchors + code style (P0 #4.2, #4.3)
- Update `prompt_builder.py`: append numeric output anchors to B-206 section
- Update `prompt_builder.py`: add new "代码风格纪律" section
- Run `pytest tests/unit/test_prompt_builder.py -v`

### Commit 3: System prompt refactoring (P1 #4.4)
- Refactor `prompt_builder.py`: extract `_STATIC_SECTIONS` and `_DYNAMIC_SECTIONS`
- Add `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` constant
- Verify `test_v2_prompt_builder.py` passes
- Verify no functional regression in `test_agent_loop.py`

### Commit 4: Push all commits
- `git push origin fix/test-failures-batch-2026-05-20`

---

## 6. Metrics to Track

After deploying these changes, monitor:

1. **Tool selection accuracy**: % of turns where the agent picks the right tool without fallback
2. **Output token efficiency**: avg tokens per turn (target: -5% from numeric anchors)
3. **Over-commenting rate**: % of file writes that include comments on unchanged code (target: <5%)
4. **Plan approval gate usage**: % of multi-step tasks that trigger `ask_user_question` before execution
5. **User satisfaction**: qualitative — fewer "你刚才不是说了做了吗" moments

---

## 7. Appendix: free-code Files Read

| File | Lines | Key Insight |
|------|-------|-------------|
| `src/constants/prompts.ts` | 914 | System prompt assembly, static/dynamic boundary, output efficiency |
| `src/constants/systemPromptSections.ts` | 68 | `systemPromptSection()` / `DANGEROUS_uncachedSystemPromptSection()` abstractions |
| `src/constants/xml.ts` | 86 | 30+ XML tag constants for structured content |
| `src/tools/BashTool/prompt.ts` | 369 | Dynamic tool description with sandbox, git safety, background tasks |
| `src/tools/AgentTool/prompt.ts` | 287 | Fork semantics, "Writing the prompt" guidance, examples |
| `src/tools/BriefTool/prompt.ts` | 22 | `SendUserMessage` as the user-facing channel, proactive status |
| `src/tools/FileEditTool/prompt.ts` | 28 | Pre-read requirement, uniqueness hint, replace_all guidance |
| `src/tools/FileReadTool/prompt.ts` | 49 | Line format instructions, offset guidance, PDF support |
| `src/tools/AskUserQuestionTool/prompt.ts` | 44 | Preview feature, plan mode note |
| `src/tools/TaskCreateTool/prompt.ts` | 56 | "When to Use / When NOT to Use" canonical example |
| `src/tools/SkillTool/prompt.ts` | 241 | BLOCKING REQUIREMENT, skill budget, description truncation |
| `src/services/compact/prompt.ts` | 374 | NO_TOOLS_PREAMBLE, `<analysis>` scaffold, 9-section summary |
| `src/services/extractMemories/prompts.ts` | 154 | Two-phase save, four-type taxonomy, turn budget |
| `src/services/SessionMemory/prompts.ts` | 324 | 10-section template, structural preservation rules, token budgets |
| `src/services/MagicDocs/prompts.ts` | 127 | BE TERSE philosophy, current-state-not-changelog |
| `CLAUDE.md` | 47 | Project overview (minimal — the real guidance is in code) |
