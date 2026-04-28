'use strict';

/**
 * PCEC 定时调度器 (xm-auto-evo 版)
 * 
 * 移植自 xm-evo/src/pcec/scheduler.js
 * 
 * 管理自动进化周期的定时触发，支持适应性间隔调整。
 */

class AutoEvoScheduler {
  /**
   * @param {Object} [options]
   * @param {number} [options.intervalMs] - 周期间隔（毫秒），默认 30 分钟
   * @param {number} [options.minIntervalMs] - 最小间隔（毫秒），默认 10 分钟
   * @param {number} [options.maxIntervalMs] - 最大间隔（毫秒），默认 4 小时
   * @param {Function} [options.onCycle] - 周期回调函数
   */
  constructor(options = {}) {
    this.intervalMs = options.intervalMs || 30 * 60 * 1000; // 30min default
    this.minIntervalMs = options.minIntervalMs || 10 * 60 * 1000; // 10min
    this.maxIntervalMs = options.maxIntervalMs || 4 * 60 * 60 * 1000; // 4h
    this.timer = null;
    this.running = false;
    this.cycleCallback = options.onCycle || null;
    this.cycleCount = 0;
    this.intervalId = null;
  }

  _scheduleNext() {
    if (!this.running) return;
    this.intervalId = setTimeout(async () => {
      this.cycleCount += 1;
      console.log(`\n🧬 [HEARTBEAT #${this.cycleCount}] ${new Date().toISOString()}`);
      if (this.cycleCallback) {
        try {
          await this.cycleCallback(this.cycleCount);
        } catch (e) {
          console.error('Cycle error:', e.message);
        }
      }
      this._scheduleNext();
    }, this.intervalMs);
  }

  /** 启动定时器 */
  start() {
    if (this.running) return;
    this.running = true;
    console.log(`\n🫀 XM-AUTO-EVO 心跳已启动 (间隔: ${this.intervalMs / 60000}min)`);
    this._scheduleNext();
  }

  /** 停止定时器 */
  stop() {
    this.running = false;
    if (this.intervalId) {
      clearTimeout(this.intervalId);
      this.intervalId = null;
    }
  }

  /** 执行单次周期 */
  async runOnce() {
    this.cycleCount += 1;
    if (typeof this.cycleCallback === 'function') {
      return await this.cycleCallback(this.cycleCount);
    }
    return null;
  }

  /** 适应性调整间隔 */
  adjustInterval(activityLevel) {
    switch (activityLevel) {
      case 'active':
        this.intervalMs = Math.max(this.minIntervalMs, Math.floor(this.intervalMs * 0.8));
        break;
      case 'idle':
        // 不变
        break;
      case 'saturated':
        this.intervalMs = Math.min(this.maxIntervalMs, Math.floor(this.intervalMs * 1.3));
        break;
    }
  }

  /** 获取调度器状态 */
  getStatus() {
    return {
      running: this.running,
      intervalMs: this.intervalMs,
      cycleCount: this.cycleCount,
      minIntervalMs: this.minIntervalMs,
      maxIntervalMs: this.maxIntervalMs,
    };
  }
}

module.exports = { AutoEvoScheduler };
