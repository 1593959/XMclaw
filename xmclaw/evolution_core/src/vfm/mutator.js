'use strict';

/**
 * VFM 权重突变器 (VFM Mutator)。
 *
 * 移植自 xm-evo/src/vfm/mutator.js
 *
 * 根据近期进化成果自适应调整 VFM 权重，使价值函数自身也能随环境演化。
 */

const { loadWeights, saveWeights, DEFAULT_WEIGHTS } = require('./scorer');
const { loadCapsules } = require('../gep/store');
const { loadEvents } = require('../gep/store');

const MAX_ADJUSTMENT = 0.5;
const WEIGHT_MIN = 1;
const WEIGHT_MAX = 5;

function clampWeight(w) {
  return Math.min(Math.max(w, WEIGHT_MIN), WEIGHT_MAX);
}

function computeSuccessRate(capsules) {
  if (!capsules || capsules.length === 0) return 1.0;
  const passed = capsules.filter(c => c.metrics?.validation_passed).length;
  return passed / capsules.length;
}

function isLowInnovation(capsules) {
  if (capsules.length < 5) return false;
  const innovateCount = capsules.filter(c => c.mutation_category === 'innovate').length;
  return (innovateCount / capsules.length) < 0.1;
}

function hasCapabilityGrowth(events) {
  if (!events || events.length === 0) return false;
  return events.some(e => e.event_type === 'capability_grown');
}

/**
 * 根据进化成果微调 VFM 权重。
 *
 * @param {Object} [currentWeights] - 当前权重（默认从文件加载）
 * @param {Object[]} [recentCapsules] - 近期 Capsule（默认自动加载）
 * @param {Object[]} [recentEvents] - 近期事件（默认自动加载）
 * @returns {Object} 调整后的权重
 */
function mutateWeights(currentWeights, recentCapsules, recentEvents) {
  const weights = { ...(currentWeights || loadWeights()) };
  const capsules = recentCapsules || loadCapsules().slice(-10);
  const events = recentEvents || loadEvents().slice(-20);
  const successRate = computeSuccessRate(capsules);

  // 近期成功率高但创新少 -> 降低 failReduce，提升 frequency
  if (successRate > 0.8 && isLowInnovation(capsules)) {
    weights.failReduce = clampWeight(weights.failReduce - MAX_ADJUSTMENT);
    weights.frequency = clampWeight(weights.frequency + MAX_ADJUSTMENT);
  }
  // 近期失败率高 -> 提升 failReduce
  else if (successRate < 0.6 && capsules.length >= 3) {
    weights.failReduce = clampWeight(weights.failReduce + MAX_ADJUSTMENT);
    weights.frequency = clampWeight(weights.frequency - MAX_ADJUSTMENT * 0.5);
  }

  // 有新能力生长事件 -> 提升 selfCost（重视简洁性）
  if (hasCapabilityGrowth(events)) {
    weights.selfCost = clampWeight(weights.selfCost + MAX_ADJUSTMENT);
  }

  saveWeights(weights);
  return weights;
}

/**
 * 获取当前 VFM 权重。
 */
function getWeights() {
  return loadWeights();
}

/**
 * 重置权重到默认值。
 */
function resetWeights() {
  saveWeights({ ...DEFAULT_WEIGHTS });
  return { ...DEFAULT_WEIGHTS };
}

module.exports = { mutateWeights, getWeights, resetWeights };
