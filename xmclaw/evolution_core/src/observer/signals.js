'use strict';

/**
 * CoPaw 对话信号提取器 (xm-auto-evo 版)
 *
 * 从 CoPaw 真实对话数据源提取进化信号。
 * 增强版（2026-04-13）：添加用户意图和主题模式提取。
 *
 * 数据源（按优先级）：
 * 1. dialog/YYYY-MM-DD.jsonl   — 最新对话记录（扁平 JSONL 格式）
 * 2. sessions/default_*.json   — 会话历史（含工具调用）
 */

const fs = require('node:fs');
const path = require('path');

// CoPaw 对话格式（dialog/）：
// { id, name, role, content: [{type, text|thinking|tool_use|tool_result, ...}], metadata, timestamp }
// role: user | assistant | system
// content[].type: text | thinking | tool_use | tool_result

// CoPaw sessions 格式：
// sessions/default_*.json → { agent: { memory: { content: [[userMsg, [assistantMsgs...]], ...] } } }

const FEATURE_KEYWORDS = /\b(功能|能力|添加|新能力|新技能|新功能|feature|ability|add)\b/i;
const CAPABILITY_GAP_KEYWORDS = /缺少|没有|能不能|希望|想要/i;
const REPETITION_KEYWORDS = /一直|总是|重复|每次/i;
const ERROR_KEYWORDS = /错误|失败|报错|exception|error|err/i;

// ──────────────────────────────────────────────
// 工具分类定义
// ──────────────────────────────────────────────
const TOOL_CATEGORIES = {
  browser:     ['browser_use', 'browser_visible', 'browser_cdp', 'desktop_screenshot'],
  file:        ['read_file', 'write_file', 'edit_file', 'glob_search', 'grep_search', 'execute_shell_command', 'send_file_to_user'],
  web_search:  ['tavily_search', 'tavily_research', 'tavily_crawl', 'tavily_extract', 'web_search'],
  memory:      ['memory_search'],
  time:        ['get_current_time', 'set_user_timezone'],
  spreadsheet: ['xlsx', 'pdf', 'docx', 'pptx'],
  code:        ['execute_shell_command'],
  cron:        ['cron'],
  agent:       ['multi_agent_collaboration'],
};

/**
 * 从 content 提取纯文本。
 *
 * 兼容两种格式：
 *   - CoPaw（旧）: content 是数组 [{type:'text'|'thinking', text|thinking}, ...]
 *   - XMclaw（新, B-16）: content 直接是字符串
 *
 * 这两种格式我们都吃，因为 xm-auto-evo 适配 XMclaw 的同时不破坏 CoPaw 兼容。
 */
function extractText(content) {
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return '';
  return content
    .filter(c => c.type === 'text' || c.type === 'thinking')
    .map(c => c.type === 'thinking' ? `[思考]${c.thinking || ''}` : (c.text || ''))
    .join(' ');
}

/**
 * 从一条对话条目提取所有工具调用。
 *
 * 兼容两种格式：
 *   - CoPaw: content 数组里有 {type:'tool_use', name, input, id} 块
 *   - XMclaw: 顶层 entry.tool_calls = [{id, name, args}]
 *
 * 接受单参数（兼容老调用：传 content[]）或双参数（传完整 entry）。
 */
function extractTools(contentOrEntry, maybeEntry) {
  // 双参数形式：(content, entry) — 老代码可能没传
  // 单参数形式：直接传 entry 或 content
  let content = null;
  let entry = null;

  if (maybeEntry !== undefined) {
    content = contentOrEntry;
    entry = maybeEntry;
  } else if (Array.isArray(contentOrEntry)) {
    content = contentOrEntry;
  } else if (contentOrEntry && typeof contentOrEntry === 'object') {
    entry = contentOrEntry;
    content = entry.content;
  }

  // XMclaw 格式：顶层 tool_calls
  if (entry && Array.isArray(entry.tool_calls) && entry.tool_calls.length > 0) {
    return entry.tool_calls.map(tc => ({
      name: tc.name,
      input: tc.args || tc.input || {},
      id: tc.id,
    }));
  }

  // CoPaw 格式：content 数组里嵌的 tool_use 块
  if (Array.isArray(content)) {
    return content
      .filter(c => c.type === 'tool_use')
      .map(c => ({ name: c.name, input: c.input || {}, id: c.id }));
  }

  return [];
}

/**
 * 工具名称 → 分类
 */
function getToolCategories(toolName) {
  const cats = [];
  for (const [cat, tools] of Object.entries(TOOL_CATEGORIES)) {
    if (tools.includes(toolName)) cats.push(cat);
  }
  return cats;
}

/**
 * 从一条 CoPaw 对话条目提取信号
 */
function extractSignalsFromEntry(entry) {
  const signals = new Set();
  // 兼容 XMclaw（content=string）和 CoPaw（content=array）两种 shape。
  // extractText/extractTools 已经在自己里面分支判断。
  const content = entry.content;

  const text = extractText(content);
  const tools = extractTools(content, entry);

  // ── 意图推断 ─────────────────────────────────
  if (FEATURE_KEYWORDS.test(text)) {
    signals.add('capability_gap');
    signals.add('intent:feature');
  }
  if (CAPABILITY_GAP_KEYWORDS.test(text)) {
    signals.add('capability_gap');
  }
  if (REPETITION_KEYWORDS.test(text)) {
    signals.add('repetitive_intent');
  }
  if (ERROR_KEYWORDS.test(text)) {
    signals.add('error_signal');
  }

  // ── 工具使用 → 信号 ─────────────────────────
  for (const tool of tools) {
    signals.add(`tool_use:${tool.name}`);

    // 工具 → 分类
    const cats = getToolCategories(tool.name);
    for (const cat of cats) {
      signals.add(`tool_category:${cat}`);
    }

    // 特殊信号
    if (tool.name === 'read_file') signals.add('intent:file');
    if (tool.name === 'write_file') signals.add('intent:file');
    if (tool.name === 'grep_search') signals.add('intent:search');
    if (tool.name === 'tavily_search') signals.add('intent:search');
    if (tool.name === 'tavily_research') signals.add('intent:research');
    if (tool.name === 'get_current_time') signals.add('intent:time');
    if (tool.name === 'cron') signals.add('intent:scheduling');
    if (tool.name === 'browser_use') signals.add('intent:browse');
  }

  // ── 无辅助响应检测 ──────────────────────────
  // 兼容两种 shape：CoPaw 数组 vs XMclaw 字符串
  const contentArr = Array.isArray(content) ? content : [];
  const hasAssistantText = contentArr.some(c => c && c.type === 'text' && c.role === 'assistant')
    || (entry.role === 'assistant' && typeof content === 'string' && content.length > 0);
  const hasToolUse = tools.length > 0;

  if (!hasToolUse && text.length > 20) {
    signals.add('unassisted_user_query');
  }
  if (hasToolUse) {
    signals.add('tool_assisted_response');
  }

  // ── 工具链模式 ───────────────────────────────
  const toolNames = tools.map(t => t.name);
  for (let i = 0; i < toolNames.length - 1; i++) {
    signals.add(`tool_chain:${toolNames[i]}→${toolNames[i + 1]}`);
    for (let j = i + 2; j < toolNames.length; j++) {
      signals.add(`tool_chain:${toolNames[i]}→${toolNames[i + 1]}→${toolNames[j]}`);
    }
  }

  // ── 错误信号提取 ────────────────────────────
  // CoPaw: content[] 里 type='tool_result' 块
  // XMclaw: 整个 entry 是 role='tool' 行，content=字符串，is_error=true
  const errorTexts = [];
  if (Array.isArray(content)) {
    for (const c of content) {
      if (c.type === 'tool_result' && c.text) errorTexts.push(c.text);
    }
  }
  if (entry.role === 'tool' && typeof content === 'string') {
    if (entry.is_error || ERROR_KEYWORDS.test(content)) {
      errorTexts.push(content);
    }
  }
  for (const errText of errorTexts) {
    if (ERROR_KEYWORDS.test(errText)) {
      signals.add('log_error');
      const errMatch = errText.match(/Error[:：]\s*([^\n]+)/);
      if (errMatch) {
        const errType = errMatch[1].replace(/[^a-zA-Z0-9]/g, '_').slice(0, 30);
        signals.add(`errsig:${errType}`);
      }
    }
  }

  // ── 复杂工作流检测 ─────────────────────────
  if (toolNames.length >= 3) {
    signals.add('complex_workflow_detected');
  }

  return [...signals];
}

/**
 * 加载 dialog/ 目录的对话条目
 */
function loadFromDialogDir(dialogDir, maxDays = 7) {
  const entries = [];
  const now = new Date();

  try {
    if (!fs.existsSync(dialogDir)) return entries;

    const files = fs.readdirSync(dialogDir).filter(f => f.endsWith('.jsonl'));

    // 按日期排序，取最新的 maxDays 个
    const sorted = files
      .map(f => {
        const match = f.match(/(\d{4}-\d{2}-\d{2})/);
        const date = match ? new Date(match[1]) : new Date(0);
        return { file: f, date };
      })
      .filter(f => (now - f.date) / 86400000 <= maxDays)
      .sort((a, b) => b.date - a.date);

    for (const { file } of sorted) {
      const filepath = path.join(dialogDir, file);
      const lines = fs.readFileSync(filepath, 'utf-8').trim().split('\n').filter(Boolean);

      for (const line of lines) {
        try {
          const entry = JSON.parse(line);
          entries.push(entry);
        } catch {}
      }
    }
  } catch (e) {
    // 忽略读取错误
  }

  return entries;
}

/**
 * 从 sessions/*.json 提取信号（CoPaw sessions 格式）
 */
function extractFromSessions(sessionsDir, maxFiles = 10) {
  const signals = [];

  try {
    if (!fs.existsSync(sessionsDir)) return signals;

    const files = fs.readdirSync(sessionsDir)
      .filter(f => f.startsWith('default_') && f.endsWith('.json'))
      .slice(-maxFiles);

    for (const file of files) {
      const filepath = path.join(sessionsDir, file);
      const raw = fs.readFileSync(filepath, 'utf-8');

      // sessions 格式: { agent: { memory: { content: [[userMsg, [assistantMsgs...]], ...] } } }
      let sessions;
      try {
        sessions = JSON.parse(raw);
      } catch {
        continue;
      }

      const content = sessions?.agent?.memory?.content;
      if (!Array.isArray(content)) continue;

      for (const item of content) {
        if (!Array.isArray(item)) continue;
        const [userMsg, assistantMsgs] = item;

        // 提取用户消息信号
        if (typeof userMsg === 'string') {
          signals.push(...extractSignalsFromEntry({ content: [{ type: 'text', text: userMsg }] }));
        } else if (userMsg?.content) {
          const entrySignals = extractSignalsFromEntry(userMsg);
          signals.push(...entrySignals);
        }

        // 提取助手消息信号
        if (Array.isArray(assistantMsgs)) {
          for (const asst of assistantMsgs) {
            if (!asst) continue;
            const entrySignals = extractSignalsFromEntry(asst);
            signals.push(...entrySignals);
          }
        }
      }
    }
  } catch (e) {
    // 忽略读取错误
  }

  return signals;
}

/**
 * 聚合所有信号来源
 */
function extractSignals(workspace) {
  const dialogDir = path.join(workspace, 'dialog');
  const sessionsDir = path.join(workspace, 'sessions');

  const allSignals = [];

  // 1. dialog/ 目录
  const dialogEntries = loadFromDialogDir(dialogDir, 7);
  for (const entry of dialogEntries) {
    allSignals.push(...extractSignalsFromEntry(entry));
  }

  // 2. sessions/ 目录
  const sessionSignals = extractFromSessions(sessionsDir, 10);
  allSignals.push(...sessionSignals);

  // 去重
  return [...new Set(allSignals)];
}

/**
 * 获取信号统计
 */
function getSignalStats(signals) {
  const total = signals.length;
  const unique = [...new Set(signals)].length;

  // 按前缀分组
  const categories = {};
  for (const sig of signals) {
    const [prefix] = sig.split(':');
    categories[prefix] = (categories[prefix] || 0) + 1;
  }

  return { total, unique, categories };
}

// ──────────────────────────────────────────────
// 增强功能：用户意图提取（2026-04-13）
// ──────────────────────────────────────────────

/**
 * 从用户文本提取具体意图
 */
function extractUserIntent(text) {
  const intents = [];
  if (!text) return intents;

  // 1. 搜索类意图
  if (/搜索|查询|找一下|帮我查|look up|search|搜一下|查一下/i.test(text)) {
    intents.push('intent:search');
    const topicMatch = text.match(/(?:关于|有关)([^，。!！.]{2,20})/i) ||
                       text.match(/(?:是什么|what is)[^?？]{0,30}[?？]?$/i);
    if (topicMatch) {
      const topic = topicMatch[1].trim().slice(0, 20);
      if (topic.length >= 2) intents.push(`search_topic:${topic}`);
    }
  }

  // 2. 工具/功能类意图
  const toolIntents = [
    [/(?:打开|访问|浏览)[^\.。]{0,20}网站/i, 'intent:web_browse'],
    [/(?:定时|schedule|提醒|cron)/i, 'intent:scheduling'],
    [/(?:分析|对比|比较|评估)/i, 'intent:analysis'],
    [/(?:文件|读取|写入|创建)/i, 'intent:file'],
    [/(?:代码|脚本|程序)/i, 'intent:code'],
  ];
  for (const [pattern, intent] of toolIntents) {
    if (pattern.test(text)) intents.push(intent);
  }

  // 3. 记忆/检索类意图
  if (/记得|记住|之前|过去|历史上|曾经的|检索|查找记忆/i.test(text)) {
    intents.push('intent:memory_retrieval');
    intents.push('capability_candidate:memory_retrieval');
  }

  // 4. 搜索工具问题
  if (/(?:搜索|search)[\s]*(?:不行|失败|不可用)/i.test(text)) {
    intents.push('capability_candidate:web_search');
    intents.push('tool_issue:search');
  }

  return intents;
}

/**
 * 检测用户重复询问模式
 */
function extractTopicPatterns(entries) {
  const userTexts = [];
  for (const entry of entries) {
    if (entry.role === 'user') {
      const text = extractText(entry.content);
      if (text.length > 5) userTexts.push(text);
    }
  }

  const patterns = [];
  const seen = new Map();

  for (const text of userTexts) {
    const keywords = text
      .replace(/[^\u4e00-\u9fa5a-zA-Z0-9]/g, ' ')
      .split(/\s+/)
      .filter(w => w.length >= 2)
      .filter(w => !['这个', '那个', '什么', '怎么', '如何', '能否', '可以', '帮我'].includes(w));

    if (keywords.length === 0) continue;

    const key = keywords.slice(0, 3).join('_');
    if (!seen.has(key)) {
      seen.set(key, { count: 0, texts: [] });
    }
    const entry = seen.get(key);
    entry.count++;
    if (entry.texts.length < 3) entry.texts.push(text.slice(0, 100));
  }

  for (const [key, entry] of seen) {
    if (entry.count >= 2) {
      patterns.push({
        type: 'repetitive_topic',
        signature: key,
        confidence: Math.min(entry.count / 10, 1),
        example: entry.texts[0] || '',
        category: 'optimize',
        count: entry.count,
      });
    }
  }

  return patterns;
}

/**
 * 增强版信号提取（整合意图和主题模式）
 */
function extractEnhancedSignals(entries) {
  const signals = [];

  for (const entry of entries) {
    // 原有信号
    signals.push(...extractSignalsFromEntry(entry));

    // 用户意图
    if (entry.role === 'user') {
      const text = extractText(entry.content);
      signals.push(...extractUserIntent(text));
    }
  }

  return signals;
}

module.exports = {
  extractSignals,
  extractFromSessions,
  extractSignalsFromEntry,
  getSignalStats,
  extractText,
  extractTools,
  loadFromDialogDir,
  extractUserIntent,
  extractTopicPatterns,
  extractEnhancedSignals,
};