'use strict';

/**
 * PCEC 周期管理。
 *
 * 移植自 xm-evo/src/pcec/cycle.js
 *
 * 管理单个 PCEC 周期的生命周期，跟踪产出，
 * 检测停滞并提供强制突破机制。
 */

const crypto = require('node:crypto');

/** 判定非实质产出的排除关键词 */
const EXCLUDE_KEYWORDS = ['总结', '回顾', '复述', '没有明显', '格式', '措辞', 'summary', 'review'];

/** 有效产出类型 */
const SUBSTANTIVE_TYPES = ['capability', 'abstraction', 'leverage', 'skill'];

/** 全局停滞计数 */
let stagnantCount = 0;

/**
 * 重置全局停滞计数。
 */
function resetStagnantCount() {
  stagnantCount = 0;
}

/**
 * PCEC 周期实例。
 */
class PCECCycle {
  constructor() {
    this.id = `pcec_${crypto.randomUUID().slice(0, 8)}`;
    this.started_at = new Date().toISOString();
    this.ended_at = null;
    this.outcomes = [];
    this.status = 'running';
  }

  /**
   * 记录周期产出。
   */
  addOutcome(outcome) {
    if (!outcome || typeof outcome !== 'object') return;
    this.outcomes.push({
      type: outcome.type || 'unknown',
      description: outcome.description || '',
      added_at: new Date().toISOString(),
    });
  }

  /**
   * 结束周期，判定是否有实质产出。
   *
   * @returns {{ substantive: boolean, stagnant_count: number }}
   */
  complete() {
    this.ended_at = new Date().toISOString();
    const substantive = this.hasSubstantiveOutcome();

    if (substantive) {
      this.status = 'completed';
      resetStagnantCount();
    } else {
      this.status = 'stagnant';
      stagnantCount++;
    }

    return { substantive, stagnant_count: stagnantCount };
  }

  /**
   * 检查周期产出是否有实质性内容。
   *
   * @returns {boolean}
   */
  hasSubstantiveOutcome() {
    if (!this.outcomes || this.outcomes.length === 0) return false;

    const validTypeOutcomes = this.outcomes.filter(o => SUBSTANTIVE_TYPES.includes(o.type));
    if (validTypeOutcomes.length === 0) return false;

    const substantiveOutcomes = validTypeOutcomes.filter(o => {
      if (!o.description || typeof o.description !== 'string') return false;
      return !EXCLUDE_KEYWORDS.some(kw => o.description.includes(kw));
    });

    return substantiveOutcomes.length > 0;
  }

  /**
   * 获取周期摘要。
   */
  getSummary() {
    return {
      id: this.id,
      status: this.status,
      started_at: this.started_at,
      ended_at: this.ended_at,
      outcome_count: this.outcomes.length,
      stagnant_count: stagnantCount,
      is_stagnant: stagnantCount >= 3,
    };
  }
}

/**
 * 获取当前全局停滞计数。
 */
function getStagnantCount() {
  return stagnantCount;
}

module.exports = {
  PCECCycle,
  getStagnantCount,
  resetStagnantCount,
  SUBSTANTIVE_TYPES,
};
