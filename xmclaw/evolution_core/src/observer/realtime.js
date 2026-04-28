'use strict';

/**
 * 🔴 实时会话监控模块 (Real-time Session Monitor)
 *
 * 使用 fs.watch 监听 dialog/ 和 sessions/ 目录的变化，
 * 新对话或 session 文件写入时立即提取信号并生成事件。
 *
 * 与 xm-auto-evo 完美融合：
 * - 复用 signals.js 的 extractSignalsFromEntry / extractText
 * - 生成标准 EvolutionEvent 写入 events.jsonl
 * - engine.js observe() 阶段消费未处理的 realtime 事件
 */

const fs = require('node:fs');
const path = require('node:path');
const { extractSignalsFromEntry, extractText } = require('./signals');
const { createEvent } = require('../gep/event');
const { appendEvent } = require('../gep/store');

class RealtimeSessionMonitor {
  constructor(workspace) {
    this.workspace = workspace;
    this.dialogDir = path.join(workspace, 'dialog');
    this.sessionsDir = path.join(workspace, 'sessions');
    this.watchers = new Map();
    this.debounceTimers = new Map();
    this.debounceMs = 2000;
    this._lastFileSizes = new Map();
    this._running = false;
  }

  /**
   * 启动实时监听
   */
  start() {
    if (this._running) return this;
    this._running = true;

    // 监听 dialog/ 目录（按日期文件）
    if (fs.existsSync(this.dialogDir)) {
      this._watchDir(this.dialogDir, 'dialog');
    }

    // 监听 sessions/ 目录
    if (fs.existsSync(this.sessionsDir)) {
      this._watchDir(this.sessionsDir, 'sessions');
    }

    console.log('   🔴 实时会话监控已启动');
    return this;
  }

  /**
   * 停止所有 watcher
   */
  stop() {
    this._running = false;
    for (const [key, watcher] of this.watchers) {
      try { watcher.close(); } catch {}
      this.watchers.delete(key);
    }
    for (const timer of this.debounceTimers.values()) {
      clearTimeout(timer);
    }
    this.debounceTimers.clear();
    console.log('   🔴 实时会话监控已停止');
  }

  _watchDir(dirPath, sourceType) {
    try {
      const watcher = fs.watch(dirPath, { recursive: false }, (eventType, filename) => {
        if (!filename) return;

        // 只关心 .jsonl (dialog) 和 .json (sessions)
        const ext = path.extname(filename);
        if (sourceType === 'dialog' && ext !== '.jsonl') return;
        if (sourceType === 'sessions' && ext !== '.json') return;

        const filePath = path.join(dirPath, filename);
        this._debounce(filePath, sourceType, () => this._handleFileChange(filePath, sourceType));
      });

      this.watchers.set(dirPath, watcher);
    } catch (e) {
      console.log(`   ⚠️ 无法监听 ${dirPath}: ${e.message}`);
    }
  }

  _debounce(key, sourceType, fn) {
    const timerKey = `${sourceType}:${key}`;
    if (this.debounceTimers.has(timerKey)) {
      clearTimeout(this.debounceTimers.get(timerKey));
    }
    this.debounceTimers.set(timerKey, setTimeout(() => {
      this.debounceTimers.delete(timerKey);
      fn();
    }, this.debounceMs));
  }

  _handleFileChange(filePath, sourceType) {
    try {
      const stat = fs.statSync(filePath);
      const currentSize = stat.size;
      const lastSize = this._lastFileSizes.get(filePath) || 0;

      // 文件缩小或没变，跳过
      if (currentSize <= lastSize) {
        this._lastFileSizes.set(filePath, currentSize);
        return;
      }

      // 只读取新增内容
      const newContent = this._readNewContent(filePath, lastSize, currentSize);
      this._lastFileSizes.set(filePath, currentSize);

      if (!newContent.trim()) return;

      const signals = this._extractSignalsFromNewContent(newContent, sourceType);
      if (signals.length === 0) return;

      // 生成 realtime 事件
      const event = createEvent({
        event_type: 'realtime_signals_detected',
        payload: {
          source: sourceType,
          file: path.basename(filePath),
          signal_count: signals.length,
          signals: signals.slice(0, 20), // 限制事件大小
        },
      });

      appendEvent(event);
      console.log(`   🔴 实时监控: ${sourceType} 新增 ${signals.length} 个信号`);
    } catch (e) {
      // 静默失败，不中断主流程
    }
  }

  _readNewContent(filePath, startByte, endByte) {
    const fd = fs.openSync(filePath, 'r');
    try {
      const length = endByte - startByte;
      const buffer = Buffer.alloc(length);
      fs.readSync(fd, buffer, 0, length, startByte);
      return buffer.toString('utf-8');
    } finally {
      fs.closeSync(fd);
    }
  }

  _extractSignalsFromNewContent(newContent, sourceType) {
    const signals = [];

    if (sourceType === 'dialog') {
      // dialog 是 JSONL，每行一个 JSON 对象
      const lines = newContent.split('\n').filter(Boolean);
      for (const line of lines) {
        try {
          const entry = JSON.parse(line);
          const entrySignals = extractSignalsFromEntry(entry);
          signals.push(...entrySignals);
        } catch {}
      }
    } else if (sourceType === 'sessions') {
      // sessions 是完整 JSON 文件，增量更新时可能只拿到部分 JSON
      // 稳妥做法：尝试解析整个文件（因为 session 文件通常不大）
      try {
        // session 文件通常不大，直接读取整个文件重新解析
        const fullContent = fs.readFileSync(filePath, 'utf-8');
        const sessions = JSON.parse(fullContent);
        const content = sessions?.agent?.memory?.content;
        if (Array.isArray(content)) {
          for (const item of content) {
            if (!Array.isArray(item)) continue;
            const [userMsg, assistantMsgs] = item;
            if (typeof userMsg === 'string') {
              signals.push(...extractSignalsFromEntry({ content: [{ type: 'text', text: userMsg }] }));
            } else if (userMsg?.content) {
              signals.push(...extractSignalsFromEntry(userMsg));
            }
            if (Array.isArray(assistantMsgs)) {
              for (const asst of assistantMsgs) {
                if (!asst) continue;
                signals.push(...extractSignalsFromEntry(asst));
              }
            }
          }
        }
      } catch {}
    }

    return [...new Set(signals)];
  }
}

module.exports = { RealtimeSessionMonitor };
