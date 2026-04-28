'use strict';

/**
 * 🧠 短时记忆模块（简化版）
 * 
 * 功能：仅内存存储，不持久化
 * 会话结束后汇总到 MEMORY.md（由 LongTermMemory 处理）
 */

class ShortTermMemory {
  constructor(workspace) {
    this.workspace = workspace;
    this.entries = new Map();
    this.sessionId = `session_${Date.now()}`;
  }

  async init() {
    // 无需初始化
  }

  /**
   * 添加记忆条目（仅内存）
   */
  set(key, value) {
    this.entries.set(key, {
      value,
      timestamp: Date.now(),
      accessCount: 0
    });
  }

  /**
   * 获取记忆
   */
  get(key) {
    const entry = this.entries.get(key);
    if (entry) {
      entry.accessCount++;
    }
    return entry ? entry.value : null;
  }

  /**
   * 检查过期（24小时）
   */
  isExpired(entry, ttlHours = 24) {
    const ttlMs = ttlHours * 60 * 60 * 1000;
    return Date.now() - entry.timestamp > ttlMs;
  }

  /**
   * 清理过期条目
   */
  cleanup() {
    for (const [key, entry] of this.entries) {
      if (this.isExpired(entry)) {
        this.entries.delete(key);
      }
    }
  }

  /**
   * 获取所有条目
   */
  getAll() {
    return Array.from(this.entries.entries()).map(([key, entry]) => ({
      key,
      value: entry.value,
      timestamp: entry.timestamp,
      accessCount: entry.accessCount
    }));
  }

  /**
   * 导出为摘要（供 LongTermMemory 使用）
   */
  export() {
    const entries = this.getAll().filter(e => !this.isExpired(e));
    return {
      sessionId: this.sessionId,
      count: entries.length,
      entries: entries.map(e => ({ key: e.key, value: e.value }))
    };
  }

  /**
   * 清除所有
   */
  clear() {
    this.entries.clear();
  }
}

module.exports = ShortTermMemory;
