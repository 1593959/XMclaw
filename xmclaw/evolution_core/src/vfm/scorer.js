'use strict';

/**
 * VFM 评分器 (Value Function Scorer)。
 *
 * 移植自 xm-evo/src/vfm/scorer.js
 *
 * 对 Gene/能力节点计算综合价值分数 V-Score（0-100），
 * 基于四个维度：复用频率、降低失败率、降低用户心智负担、降低自身推理成本。
 */

const { loadGenes, loadCapsules } = require('../gep/store');

/** 进化价值阈值，低于此分数不值得进化 */
const EVOLUTION_THRESHOLD = 20;

/** 默认权重（可被 VFM Mutator 调整） */
const DEFAULT_WEIGHTS = {
  frequency: 3,
  failReduce: 3,
  userBurden: 2,
  selfCost: 2,
};

/** 权重配置文件路径 */
const _path = require('path');
const _SCORER_DATA_DIR = process.env.WORKSPACE
  ? _path.join(process.env.WORKSPACE, 'data')
  : _path.join(__dirname, '..', '..', 'data');
const WEIGHTS_FILE = _path.join(_SCORER_DATA_DIR, 'vfm_weights.json');

const fs = require('node:fs');

function loadWeights() {
  try {
    if (fs.existsSync(WEIGHTS_FILE)) {
      const raw = fs.readFileSync(WEIGHTS_FILE, 'utf-8').trim();
      if (raw) return { ...DEFAULT_WEIGHTS, ...JSON.parse(raw) };
    }
  } catch {}
  return { ...DEFAULT_WEIGHTS };
}

function saveWeights(weights) {
  const dir = require('path').dirname(WEIGHTS_FILE);
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(WEIGHTS_FILE, JSON.stringify(weights, null, 2) + '\n', 'utf-8', 'utf-8', 'utf-8');
}

/**
 * 频率维度评分 (0-10)。
 * 使用 log2(trigger_count + 1) 归一化，上限为 10。
 * 新 Gene 保底 3 分，避免完全无法进化。
 */
function scoreFrequency(node) {
  const count = node.trigger_count || 0;
  return Math.max(Math.min(Math.log2(count + 1), 10), 3);
}

/**
 * 降低失败率维度评分 (0-10)。
 * 相关 Capsule 的 validation_passed 成功率映射。
 */
function scoreFailReduction(node, capsules) {
  if (!capsules || capsules.length === 0) return 3; // 新 Gene 保底 3 分
  const linkedGenes = new Set(node.linked_genes || []);
  const relatedCapsules = capsules.filter(c => linkedGenes.has(c.gene_id));
  if (relatedCapsules.length === 0) return 3; // 无相关 Capsule 保底 3 分
  const passedCount = relatedCapsules.filter(c => c.metrics?.validation_passed).length;
  return Math.max((passedCount / relatedCapsules.length) * 10, 3);
}

/**
 * 降低用户心智负担维度评分 (0-10)。
 * 有 linked_skills 且 preconditions 少得高分。
 */
function scoreUserBurden(node) {
  const skillCount = (node.linked_skills || []).length;
  const precondCount = (node.preconditions || []).length;
  let score = Math.min(skillCount * 2, 6);
  score += Math.max(4 - precondCount, 0);
  return Math.max(Math.min(score, 10), 3);
}

/**
 * 降低自身推理成本维度评分 (0-10)。
 * 关联 Gene 的 strategy 步骤少则得高分。
 */
function scoreSelfCost(node) {
  const linkedGeneIds = node.linked_genes || [];
  if (linkedGeneIds.length === 0) return 5;

  const genes = loadGenes();
  const geneMap = {};
  for (const g of genes) geneMap[g.id] = g;

  let totalSteps = 0;
  let geneCount = 0;

  for (const geneId of linkedGeneIds) {
    const gene = geneMap[geneId];
    if (gene?.strategy?.steps) {
      totalSteps += gene.strategy.steps.length;
      geneCount++;
    }
  }

  if (geneCount === 0) return 5;
  const avgSteps = totalSteps / geneCount;
  // 步骤越少越好：avgSteps=1 得10分，avgSteps>=10 得0分，保底3分
  return Math.max(3, Math.min(10, 10 - (avgSteps - 1) * (10 / 9)));
}

/**
 * 计算节点综合 V-Score（0-100）。
 *
 * @param {Object} node - 能力节点或 Gene
 * @param {Object} [weights] - 可选的四维权重
 * @returns {number} 0-100 的分数
 */
function computeVScore(node, weights) {
  const w = weights || loadWeights();
  const freq = scoreFrequency(node);
  const failRed = scoreFailReduction(node, loadCapsules());
  const userBur = scoreUserBurden(node);
  const selfCost = scoreSelfCost(node);

  const raw = freq * w.frequency
    + failRed * w.failReduce
    + userBur * w.userBurden
    + selfCost * w.selfCost;

  // 归一化到 0-100
  const totalWeight = w.frequency + w.failReduce + w.userBurden + w.selfCost;
  return Math.round((raw / (totalWeight * 10)) * 100);
}

/**
 * 判断能力是否值得进化。
 *
 * @param {Object} node - 能力节点或 Gene
 * @returns {{ worth: boolean, score: number, threshold: number }}
 */
function isWorthEvolving(node) {
  const score = computeVScore(node);
  return { worth: score >= EVOLUTION_THRESHOLD, score, threshold: EVOLUTION_THRESHOLD };
}

module.exports = {
  computeVScore,
  isWorthEvolving,
  EVOLUTION_THRESHOLD,
  DEFAULT_WEIGHTS,
  loadWeights,
  saveWeights,
  scoreFrequency,
  scoreFailReduction,
  scoreUserBurden,
  scoreSelfCost,
};
