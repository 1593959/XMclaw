'use strict';

/**
 * 🔀 跨会话监控模块 (Cross-Session Monitor)
 *
 * 按 session 分组分析 dialog/ 和 sessions/ 数据，
 * 检测跨会话的重复模式、用户偏好演变、长期趋势。
 *
 * 与 xm-auto-evo 完美融合：
 * - 输出格式与 pattern.js 的 PatternMatcher 完全兼容
 * - 直接为 engine.js observe() 提供跨会话 patterns
 * - 生成 cross_session_insight 事件供 medium.js 消费
 */

const fs = require('node:fs');
const path = require('node:path');
const { extractSignalsFromEntry, extractText } = require('./signals');
const { createEvent } = require('../gep/event');
const { appendEvent } = require('../gep/store');

class CrossSessionAnalyzer {
  constructor(workspace) {
    this.workspace = workspace;
    this.dialogDir = path.join(workspace, 'dialog');
    this.sessionsDir = path.join(workspace, 'sessions');
    this._cachePath = path.join(workspace, 'skills', 'xm-auto-evo', 'data', 'cross_session_cache.json');
    this._cache = this._loadCache();
  }

  _loadCache() {
    try {
      if (fs.existsSync(this._cachePath)) {
        return JSON.parse(fs.readFileSync(this._cachePath, 'utf-8'));
      }
    } catch {}
    return { sessions: {}, lastAnalyzedAt: null };
  }

  _saveCache() {
    try {
      fs.mkdirSync(path.dirname(this._cachePath), { recursive: true });
      fs.writeFileSync(this._cachePath, JSON.stringify(this._cache, null, 2) + '\n', 'utf-8');
    } catch {}
  }

  /**
   * 执行跨会话分析
   * @returns {{ patterns: Array, insights: Array, sessionsAnalyzed: number }}
   */
  analyze(maxSessionFiles = 50) {
    const sessions = this._loadSessions(maxSessionFiles);
    const dialogBySession = this._loadDialogMappedBySession();

    const allSessions = this._mergeSessionData(sessions, dialogBySession);

    if (Object.keys(allSessions).length === 0) {
      return { patterns: [], insights: [], sessionsAnalyzed: 0 };
    }

    const patterns = [];
    const insights = [];

    patterns.push(...this._detectCrossSessionIntents(allSessions));
    patterns.push(...this._detectCrossSessionTools(allSessions));
    patterns.push(...this._detectCrossSessionErrors(allSessions));
    insights.push(...this._detectPreferenceEvolution(allSessions));
    patterns.push(...this._detectCrossSessionGaps(allSessions));

    if (insights.length > 0 || patterns.length > 0) {
      appendEvent(createEvent({
        event_type: 'cross_session_analysis_complete',
        payload: {
          sessions_analyzed: Object.keys(allSessions).length,
          pattern_count: patterns.length,
          insight_count: insights.length,
        },
      }));
    }

    this._cache.lastAnalyzedAt = new Date().toISOString();
    this._saveCache();

    return { patterns, insights, sessionsAnalyzed: Object.keys(allSessions).length };
  }

  _loadSessions(maxFiles) {
    const sessions = {};
    try {
      if (!fs.existsSync(this.sessionsDir)) return sessions;
      const files = fs.readdirSync(this.sessionsDir).filter(f => f.endsWith('.json')).slice(-maxFiles);
      for (const file of files) {
        const sessionId = file.replace('.json', '');
        const filePath = path.join(this.sessionsDir, file);
        try {
          const data = JSON.parse(fs.readFileSync(filePath, 'utf-8'));
          const content = data?.agent?.memory?.content;
          if (!Array.isArray(content)) continue;
          const messages = [];
          for (const item of content) {
            if (!Array.isArray(item)) continue;
            const [userMsg, assistantMsgs] = item;
            if (typeof userMsg === 'string') {
              messages.push({ role: 'user', text: userMsg });
            } else if (userMsg?.content) {
              messages.push({ role: 'user', text: extractText(userMsg.content), content: userMsg.content });
            }
            if (Array.isArray(assistantMsgs)) {
              for (const asst of assistantMsgs) {
                if (!asst) continue;
                messages.push({ role: 'assistant', text: extractText(asst.content || []), content: asst.content });
              }
            }
          }
          sessions[sessionId] = { source: 'sessions', messages, file };
        } catch {}
      }
    } catch {}
    return sessions;
  }

  _loadDialogMappedBySession() {
    const dialogMap = {};
    try {
      if (!fs.existsSync(this.dialogDir)) return dialogMap;
      const files = fs.readdirSync(this.dialogDir).filter(f => f.endsWith('.jsonl')).sort();
      for (const file of files) {
        const filePath = path.join(this.dialogDir, file);
        try {
          const lines = fs.readFileSync(filePath, 'utf-8').split('\n').filter(Boolean);
          for (const line of lines) {
            try {
              const entry = JSON.parse(line);
              const sessionId = entry.metadata?.session_id || entry.session_id || 'unknown';
              if (!dialogMap[sessionId]) dialogMap[sessionId] = [];
              dialogMap[sessionId].push(entry);
            } catch {}
          }
        } catch {}
      }
    } catch {}
    return dialogMap;
  }

  _mergeSessionData(sessions, dialogBySession) {
    const merged = { ...sessions };
    for (const [sessionId, entries] of Object.entries(dialogBySession)) {
      if (!merged[sessionId]) merged[sessionId] = { source: 'dialog', messages: [] };
      for (const entry of entries) {
        const text = entry.role === 'user' ? extractText(entry.content) : '';
        if (text) {
          merged[sessionId].messages.push({ role: entry.role, text, content: entry.content, timestamp: entry.timestamp });
        }
      }
    }
    return merged;
  }

  _detectCrossSessionIntents(sessions) {
    const intentCounts = {};
    const intentSessions = {};
    for (const [sessionId, session] of Object.entries(sessions)) {
      const sessionIntents = new Set();
      for (const msg of session.messages) {
        if (msg.role !== 'user') continue;
        const lower = msg.text.toLowerCase();
        const intents = [];
        if (/搜索|查询|找一下|帮我查|查一下/.test(lower)) intents.push('search_query');
        if (/读取|写入|创建|修改|删除.*文件|文件.*操作/.test(lower)) intents.push('file_op');
        if (/写.*代码|生成.*代码|帮我写|创建一个.*脚本/.test(lower)) intents.push('code_gen');
        if (/分析|对比|比较|评估/.test(lower)) intents.push('analysis');
        if (/天气/.test(lower)) intents.push('weather');
        if (/功能|能力|添加|新能力|新技能|新功能/.test(lower)) intents.push('feature_request');
        if (/错误|失败|报错|bug|fix|修复/.test(lower)) intents.push('repair_request');
        for (const intent of intents) sessionIntents.add(intent);
      }
      for (const intent of sessionIntents) {
        intentCounts[intent] = (intentCounts[intent] || 0) + 1;
        if (!intentSessions[intent]) intentSessions[intent] = new Set();
        intentSessions[intent].add(sessionId);
      }
    }

    const patterns = [];
    const totalSessions = Object.keys(sessions).length;
    for (const [intent, count] of Object.entries(intentCounts)) {
      const sessionRatio = intentSessions[intent].size / totalSessions;
      if (sessionRatio >= 0.3 && intentSessions[intent].size >= 2) {
        patterns.push({
          type: 'cross_session_intent',
          category: 'optimize',
          signature: `cross_session_${intent}`,
          example: `在 ${intentSessions[intent].size} 个会话中检测到 ${intent} 意图`,
          confidence: Math.min(0.95, 0.6 + sessionRatio * 0.3),
          description: `跨会话重复意图: ${intent}，覆盖 ${(sessionRatio * 100).toFixed(0)}% 的会话`,
          cross_session: true,
          session_count: intentSessions[intent].size,
        });
      }
    }
    return patterns;
  }

  _detectCrossSessionTools(sessions) {
    const toolCounts = {};
    const toolSessions = {};
    for (const [sessionId, session] of Object.entries(sessions)) {
      const sessionTools = new Set();
      for (const msg of session.messages) {
        const tools = msg.content ? extractSignalsFromEntry({ content: msg.content, role: msg.role }) : [];
        for (const sig of tools) {
          if (sig.startsWith('tool_use:')) sessionTools.add(sig.replace('tool_use:', ''));
        }
      }
      for (const tool of sessionTools) {
        toolCounts[tool] = (toolCounts[tool] || 0) + 1;
        if (!toolSessions[tool]) toolSessions[tool] = new Set();
        toolSessions[tool].add(sessionId);
      }
    }
    const patterns = [];
    const totalSessions = Object.keys(sessions).length;
    for (const [tool, count] of Object.entries(toolCounts)) {
      const sessionRatio = toolSessions[tool].size / totalSessions;
      if (sessionRatio >= 0.3 && toolSessions[tool].size >= 2) {
        patterns.push({
          type: 'cross_session_tool',
          category: 'optimize',
          signature: `cross_session_tool_${tool}`,
          example: `在 ${toolSessions[tool].size} 个会话中使用 ${tool}`,
          confidence: Math.min(0.9, 0.5 + sessionRatio * 0.3),
          description: `跨会话高频工具: ${tool}，覆盖 ${(sessionRatio * 100).toFixed(0)}% 的会话`,
          cross_session: true,
          session_count: toolSessions[tool].size,
        });
      }
    }
    return patterns;
  }

  _detectCrossSessionErrors(sessions) {
    const errorCounts = {};
    const errorSessions = {};
    for (const [sessionId, session] of Object.entries(sessions)) {
      const sessionErrors = new Set();
      for (const msg of session.messages) {
        if (msg.role !== 'user') continue;
        const lower = msg.text.toLowerCase();
        const errors = [];
        if (/错误|失败|报错|exception|error|err/.test(lower)) errors.push('error_feedback');
        if (/不对|不是|不行|没实现|没做好|有问题/.test(lower)) errors.push('negative_feedback');
        if (/重复|一直|总是|又/.test(lower)) errors.push('repeated_issue');
        for (const err of errors) sessionErrors.add(err);
      }
      for (const err of sessionErrors) {
        errorCounts[err] = (errorCounts[err] || 0) + 1;
        if (!errorSessions[err]) errorSessions[err] = new Set();
        errorSessions[err].add(sessionId);
      }
    }
    const patterns = [];
    const totalSessions = Object.keys(sessions).length;
    for (const [err, count] of Object.entries(errorCounts)) {
      const sessionRatio = errorSessions[err].size / totalSessions;
      if (errorSessions[err].size >= 2) {
        patterns.push({
          type: 'cross_session_error',
          category: 'repair',
          signature: `cross_session_${err}`,
          example: `在 ${errorSessions[err].size} 个会话中检测到 ${err}`,
          confidence: Math.min(0.95, 0.7 + sessionRatio * 0.2),
          description: `跨会话问题反馈: ${err}，影响 ${errorSessions[err].size} 个会话`,
          cross_session: true,
          session_count: errorSessions[err].size,
        });
      }
    }
    return patterns;
  }

  _detectPreferenceEvolution(sessions) {
    const insights = [];
    const sessionIds = Object.keys(sessions).sort();
    if (sessionIds.length < 2) return insights;
    const preferenceKeywords = {
      direct: /直接|简洁|不要废话|简单点/,
      detailed: /详细|具体|展开说|多讲点/,
      proactive: /主动|提前|不用我说|自己想办法/,
      cautious: /先问|确认一下|不确定|先检查/,
    };
    for (const [keyword, regex] of Object.entries(preferenceKeywords)) {
      let firstSession = null;
      let repeatCount = 0;
      for (const sessionId of sessionIds) {
        const session = sessions[sessionId];
        const hasKeyword = session.messages.some(m => m.role === 'user' && regex.test(m.text));
        if (hasKeyword) {
          if (!firstSession) firstSession = sessionId;
          repeatCount++;
        }
      }
      if (firstSession && repeatCount >= 2) {
        insights.push({
          type: 'preference_evolution',
          category: 'optimize',
          signature: `preference_${keyword}`,
          confidence: Math.min(0.9, 0.5 + repeatCount * 0.1),
          description: `用户偏好演变: ${keyword}，在 ${repeatCount} 个会话中被提及`,
          cross_session: true,
          session_count: repeatCount,
        });
      }
    }
    return insights;
  }

  _detectCrossSessionGaps(sessions) {
    const gapCounts = {};
    const gapSessions = {};
    for (const [sessionId, session] of Object.entries(sessions)) {
      const sessionGaps = new Set();
      for (const msg of session.messages) {
        if (msg.role !== 'user') continue;
        const lower = msg.text.toLowerCase();
        const gaps = [];
        if (/缺少|没有|能不能|希望|想要|能否|可不可以/.test(lower)) gaps.push('capability_gap');
        if (/怎么.*没有|为什么.*不|还没.*实现/.test(lower)) gaps.push('missing_feature');
        for (const gap of gaps) sessionGaps.add(gap);
      }
      for (const gap of sessionGaps) {
        gapCounts[gap] = (gapCounts[gap] || 0) + 1;
        if (!gapSessions[gap]) gapSessions[gap] = new Set();
        gapSessions[gap].add(sessionId);
      }
    }
    const patterns = [];
    for (const [gap, count] of Object.entries(gapCounts)) {
      if (gapSessions[gap].size >= 2) {
        patterns.push({
          type: 'cross_session_gap',
          category: 'innovate',
          signature: `cross_session_${gap}`,
          example: `在 ${gapSessions[gap].size} 个会话中检测到 ${gap}`,
          confidence: Math.min(0.9, 0.5 + gapSessions[gap].size * 0.05),
          description: `跨会话能力缺口: ${gap}，在 ${gapSessions[gap].size} 个会话中被提及`,
          cross_session: true,
          session_count: gapSessions[gap].size,
        });
      }
    }
    return patterns;
  }
}

module.exports = { CrossSessionAnalyzer };