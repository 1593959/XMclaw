'use strict';

/**
 * 🧠 中时记忆模块（增强版）
 *
 * 功能：从对话和 MEMORY.md 中提取趋势、洞察和模式
 * 为进化阶段提供可行动的输入。
 */

const fs = require('node:fs');
const path = require('path');

class MediumTermMemory {
  constructor(workspace) {
    this.workspace = workspace;
    this.memoryFile = path.join(workspace, 'MEMORY.md');
    this.patterns = [];
    this.activities = [];
    this._trendCache = null;
    this._insightCache = null;
  }

  async init() {
    // 无需初始化，直接使用 MEMORY.md 和 dialog/
  }

  /**
   * 记录活动
   */
  async recordActivity(activity, metadata = {}) {
    this.activities.push({ activity, metadata, time: Date.now() });
    return { status: 'ok' };
  }

  /**
   * 记录趋势
   */
  async recordTrend(trend) {
    return { status: 'ok', trend };
  }

  /**
   * 记录模式
   */
  async recordPattern(pattern) {
    this.patterns.push(pattern);
    return { status: 'ok' };
  }

  /**
   * 从 dialog/ 读取最近 N 天的用户消息
   */
  _loadRecentUserMessages(days = 7, maxPerDay = 100) {
    const dialogDir = path.join(this.workspace, 'dialog');
    if (!fs.existsSync(dialogDir)) return [];

    const messages = [];
    const today = new Date();

    for (let i = 0; i < days; i++) {
      const d = new Date(today);
      d.setDate(d.getDate() - i);
      const dateStr = d.toISOString().split('T')[0];
      const filePath = path.join(dialogDir, `${dateStr}.jsonl`);

      if (!fs.existsSync(filePath)) continue;

      try {
        const lines = fs.readFileSync(filePath, 'utf-8').split('\n').filter(Boolean);
        let count = 0;
        for (const line of lines) {
          if (count >= maxPerDay) break;
          try {
            const entry = JSON.parse(line);
            if (entry.role === 'user') {
              const text = this._extractText(entry.content);
              if (text) {
                messages.push({ text, date: dateStr, timestamp: entry.timestamp });
                count++;
              }
            }
          } catch {}
        }
      } catch {}
    }

    return messages;
  }

  /**
   * 从 content[] 提取纯文本
   */
  _extractText(content) {
    if (!Array.isArray(content)) return typeof content === 'string' ? content : '';
    return content
      .filter(c => c.type === 'text' || c.type === 'thinking')
      .map(c => c.type === 'thinking' ? `[思考]${c.thinking || ''}` : (c.text || ''))
      .join(' ')
      .trim();
  }

  /**
   * 检测趋势（从最近对话中提取主题和意图趋势）
   */
  async detectTrends(days = 7) {
    const messages = this._loadRecentUserMessages(days, 100);
    if (messages.length === 0) return [];

    const trends = [];

    // 趋势 1：高频意图
    const intentCounts = {};
    for (const { text } of messages) {
      const lower = text.toLowerCase();
      if (/搜索|查询|找一下|帮我查/.test(lower)) intentCounts.search = (intentCounts.search || 0) + 1;
      if (/读取|写入|创建|修改|删除.*文件/.test(lower)) intentCounts.file = (intentCounts.file || 0) + 1;
      if (/写.*代码|生成.*代码|帮我写|脚本/.test(lower)) intentCounts.code = (intentCounts.code || 0) + 1;
      if (/分析|对比|比较|评估/.test(lower)) intentCounts.analysis = (intentCounts.analysis || 0) + 1;
      if (/歌|歌词|音乐|旋律|曲/.test(lower)) intentCounts.music = (intentCounts.music || 0) + 1;
      if (/进化|学习|自动|优化/.test(lower)) intentCounts.evolution = (intentCounts.evolution || 0) + 1;
      if (/修复|bug|错误|报错|失败/.test(lower)) intentCounts.repair = (intentCounts.repair || 0) + 1;
    }

    for (const [intent, count] of Object.entries(intentCounts)) {
      if (count >= 2) {
        trends.push({
          type: 'intent',
          name: intent,
          count,
          strength: Math.min(count / 5, 1.0),
          description: `最近 ${days} 天内出现 ${count} 次 ${intent} 意图`,
        });
      }
    }

    // 趋势 2：重复关键词
    const keywordCounts = {};
    const keywords = ['skill', 'gene', 'memory', '进化', '自动', '修复', '音乐', '歌词', '代码', '文件'];
    for (const { text } of messages) {
      const lower = text.toLowerCase();
      for (const kw of keywords) {
        if (lower.includes(kw.toLowerCase())) {
          keywordCounts[kw] = (keywordCounts[kw] || 0) + 1;
        }
      }
    }

    for (const [kw, count] of Object.entries(keywordCounts)) {
      if (count >= 2) {
        trends.push({
          type: 'keyword',
          name: kw,
          count,
          strength: Math.min(count / 5, 1.0),
          description: `最近 ${days} 天内出现 ${count} 次 "${kw}"`,
        });
      }
    }

    this._trendCache = trends;
    return trends;
  }

  /**
   * 生成洞察（从趋势和 MEMORY.md 中提取可行动的建议）
   */
  async generateSummary(days = 7) {
    const trends = this._trendCache || (await this.detectTrends(days));
    const messages = this._loadRecentUserMessages(days, 50);
    const insights = [];

    // 洞察 1：从意图趋势生成
    for (const trend of trends) {
      if (trend.type === 'intent' && trend.count >= 3) {
        insights.push({
          type: 'capability_gap',
          priority: trend.strength > 0.6 ? 'high' : 'medium',
          title: `高频 ${trend.name} 需求`,
          description: trend.description,
          suggestion: `考虑为 ${trend.name} 场景自动生成 Gene 或 Skill`,
          source: 'trend_analysis',
        });
      }
    }

    // 洞察 2：从用户反馈中提取（"喜欢"、"很好"、"不对"等）
    const feedbackPatterns = [];
    for (const { text } of messages) {
      const lower = text.toLowerCase();
      if (/很好|不错|喜欢|满意|棒/.test(lower)) {
        feedbackPatterns.push({ type: 'positive', text: text.slice(0, 80) });
      }
      if (/不对|不行|不好|错了|失望/.test(lower)) {
        feedbackPatterns.push({ type: 'negative', text: text.slice(0, 80) });
      }
      if (/能不能|能不能|希望|想要|建议/.test(lower)) {
        feedbackPatterns.push({ type: 'request', text: text.slice(0, 80) });
      }
    }

    if (feedbackPatterns.length > 0) {
      const positiveCount = feedbackPatterns.filter(f => f.type === 'positive').length;
      const negativeCount = feedbackPatterns.filter(f => f.type === 'negative').length;
      const requestCount = feedbackPatterns.filter(f => f.type === 'request').length;

      if (requestCount >= 2) {
        insights.push({
          type: 'user_request',
          priority: 'high',
          title: '用户有明确的新需求',
          description: `检测到 ${requestCount} 次需求表达`,
          suggestion: '分析请求内容，生成对应能力',
          source: 'feedback_analysis',
        });
      }

      if (negativeCount >= 1) {
        insights.push({
          type: 'quality_issue',
          priority: 'high',
          title: '用户反馈有不满',
          description: `检测到 ${negativeCount} 次负面反馈`,
          suggestion: '定位问题并生成修复型 Gene',
          source: 'feedback_analysis',
        });
      }
    }

    // 洞察 3：从 solidify_failed 事件中提取
    try {
      const { loadEvents } = require('../gep/store');
      const events = loadEvents().slice(-50);
      const failedEvents = events.filter(e => e.event_type === 'solidify_failed');
      if (failedEvents.length >= 1) {
        const latest = failedEvents[failedEvents.length - 1];
        const geneId = latest.payload?.gene_id || 'unknown';
        const reason = latest.payload?.reason || 'validation_or_adl_failed';
        insights.push({
          type: 'quality_issue',
          priority: 'high',
          title: `Gene ${geneId} 固化失败`,
          description: `原因: ${reason}。最近 ${failedEvents.length} 次固化失败。`,
          suggestion: '分析失败原因，修复对应 Skill 或源码',
          source: 'solidify_failure',
        });
      }
    } catch {}

    // 洞察 4：从 MEMORY.md 中的经验教训提取
    try {
      const memoryContent = fs.readFileSync(this.memoryFile, 'utf-8');
      const lessonMatches = memoryContent.match(/##?\s*.*lesson.*\n([\s\S]*?)(?=##|\n---|$)/gi);
      if (lessonMatches && lessonMatches.length >= 2) {
        insights.push({
          type: 'pattern_lesson',
          priority: 'medium',
          title: '经验教训积累较多',
          description: `MEMORY.md 中有 ${lessonMatches.length} 条教训记录`,
          suggestion: '将高频教训固化为 Gene 或检查清单',
          source: 'memory_analysis',
        });
      }
    } catch {}

    this._insightCache = insights;
    return { trends, insights };
  }

  /**
   * 将洞察写入今日日志
   */
  async writeInsightsToDailyLog(workspace) {
    const insights = this._insightCache;
    if (!insights || insights.length === 0) return false;

    const today = new Date().toISOString().split('T')[0];
    const logFile = path.join(workspace, 'memory', `${today}.md`);

    try {
      const dir = path.dirname(logFile);
      if (!fs.existsSync(dir)) {
        fs.mkdirSync(dir, { recursive: true });
      }

      let existing = '';
      if (fs.existsSync(logFile)) {
        existing = fs.readFileSync(logFile, 'utf-8');
      }

      // 过滤掉已经写入过的洞察（按标题去重）
      const newInsights = insights.filter(i => !existing.includes(`**${i.title}**`));
      if (newInsights.length === 0) return false;

      const section = `\n## 今日洞察 [${new Date().toLocaleTimeString()}]\n${newInsights.map(i => `- **${i.title}** (${i.priority})\n  - 类型: ${i.type} | 来源: ${i.source}\n  - ${i.description}\n  - 建议: ${i.suggestion}`).join('\n')}\n`;

      if (existing) {
        fs.appendFileSync(logFile, section, 'utf-8');
      } else {
        fs.writeFileSync(logFile, `# ${today} 每日笔记\n${section}\n`, 'utf-8');
      }
      return true;
    } catch (e) {
      console.error('写入今日洞察失败:', e.message);
      return false;
    }
  }

  /**
   * 获取今日摘要
   */
  async getTodaySummary() {
    try {
      const content = fs.readFileSync(this.memoryFile, 'utf-8');
      return { content, patterns: this.patterns, activities: this.activities };
    } catch (e) {
      return null;
    }
  }

  /**
   * 导出汇总
   */
  async export() {
    return {
      patterns: this.patterns,
      activities: this.activities,
      summary: `patterns: ${this.patterns.length}, activities: ${this.activities.length}`
    };
  }
}

module.exports = MediumTermMemory;