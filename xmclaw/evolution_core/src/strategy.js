'use strict';

/**
 * 进化策略预设 (xm-auto-evo 版)
 * 
 * 移植自 xm-evo/src/strategy.js
 * 
 * 定义不同场景下 innovate / optimize / repair 的权重分配，
 * 并提供基于进化历史的自动策略检测。
 */

/** @type {Record<string, StrategyConfig>} */
const STRATEGIES = {
  balanced:        { innovate: 50, optimize: 30, repair: 20, description: '日常运行' },
  innovate:        { innovate: 80, optimize: 15, repair:  5, description: '系统稳定，快速创新' },
  harden:          { innovate: 20, optimize: 40, repair: 40, description: '大改后聚焦稳固' },
  'repair-only':   { innovate:  0, optimize: 20, repair: 80, description: '紧急修复' },
  'early-stabilize': { innovate: 10, optimize: 30, repair: 60, description: '早期稳定化' },
  'steady-state':  { innovate: 40, optimize: 40, repair: 20, description: '稳态运行' },
};

/**
 * 获取策略配置
 */
function getStrategy(name) {
  const strategy = STRATEGIES[name];
  if (!strategy) {
    throw new Error(`Unknown strategy "${name}". Valid: ${Object.keys(STRATEGIES).join(', ')}`);
  }
  return { ...strategy };
}

/**
 * 根据进化历史自动检测最佳策略
 */
function autoDetectStrategy(events) {
  if (!Array.isArray(events)) return 'balanced';
  const totalCycles = events.length;

  if (totalCycles < 5) return 'early-stabilize';

  const recentEvents = events.slice(-5);
  const hasNoOutput = recentEvents.every(e => !e.mutation_applied || e.result === 'no_change' || e.result === 'skipped');
  if (hasNoOutput) return 'innovate';

  const recentWindow = events.slice(-10);
  const solidifyAttempts = recentWindow.filter(e => e.solidify_failed !== undefined || e.solidify_success !== undefined);
  if (solidifyAttempts.length > 0) {
    const failedCount = solidifyAttempts.filter(e => e.solidify_failed).length;
    const failRatio = failedCount / solidifyAttempts.length;
    if (failRatio > 0.4) return 'repair-only';
    if (failRatio > 0.2) return 'harden';
  }

  return 'balanced';
}

/**
 * 获取所有可用策略名称
 */
function getAvailableStrategies() {
  return Object.keys(STRATEGIES);
}

module.exports = { getStrategy, autoDetectStrategy, getAvailableStrategies, STRATEGIES };
