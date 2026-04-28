'use strict';

/**
 * 能力树修剪策略 (Pruner)。
 *
 * 移植自 xm-evo/src/tree/pruner.js
 *
 * 分析能力树节点，识别需要修剪或合并的候选节点。
 * 使用时间阈值和 V-Score 判断节点活跃度，使用 Jaccard 相似度检测可合并节点。
 */

const CANDIDATE_PRUNE_DAYS = 30;
const AUTO_PRUNE_DAYS = 60;
const PRUNE_VSCORE_THRESHOLD = 40;
const MERGE_SIMILARITY_THRESHOLD = 0.8;

function tokenize(name) {
  if (!name) return new Set();
  const tokens = (name || '').toLowerCase().split(/[\s.]+/).filter(Boolean);
  return new Set(tokens);
}

function jaccardSimilarity(setA, setB) {
  if (setA.size === 0 && setB.size === 0) return 1.0;
  if (setA.size === 0 || setB.size === 0) return 0.0;
  let intersection = 0;
  for (const item of setA) {
    if (setB.has(item)) intersection++;
  }
  const union = setA.size + setB.size - intersection;
  return union === 0 ? 0 : intersection / union;
}

function daysSinceTriggered(node) {
  if (!node.last_triggered) return Infinity;
  const lastMs = new Date(node.last_triggered).getTime();
  return (Date.now() - lastMs) / (24 * 60 * 60 * 1000);
}

/**
 * 分析哪些节点应该被修剪或合并。
 *
 * @param {Object[]} nodes - 所有节点（从 CapabilityTree.getAllNodes() 获取）
 * @returns {{ candidate_prune: Object[], auto_prune: Object[], merge_suggestions: Array }}
 */
function analyzePruning(nodes) {
  const candidatePrune = [];
  const autoPrune = [];
  const mergeSuggestions = [];

  const activeNodes = nodes.filter(n => n.status !== 'pruned');

  for (const node of activeNodes) {
    const days = daysSinceTriggered(node);
    const vScore = node.v_score ?? PRUNE_VSCORE_THRESHOLD + 1;

    if (days > AUTO_PRUNE_DAYS) {
      autoPrune.push({
        ...node,
        prune_reason: `未触发超过 ${AUTO_PRUNE_DAYS} 天`,
      });
    } else if (days > CANDIDATE_PRUNE_DAYS && vScore < PRUNE_VSCORE_THRESHOLD) {
      candidatePrune.push({
        ...node,
        prune_reason: `未触发 ${days.toFixed(0)} 天，且 V-Score=${vScore} < ${PRUNE_VSCORE_THRESHOLD}`,
      });
    }
  }

  // 检测名称相似度，提出合并建议
  for (let i = 0; i < activeNodes.length; i++) {
    for (let j = i + 1; j < activeNodes.length; j++) {
      const nodeA = activeNodes[i];
      const nodeB = activeNodes[j];
      if (nodeA.parent_id === nodeB.parent_id) {
        const sim = jaccardSimilarity(tokenize(nodeA.name), tokenize(nodeB.name));
        if (sim >= MERGE_SIMILARITY_THRESHOLD) {
          mergeSuggestions.push([nodeA.id, nodeB.id, sim]);
        }
      }
    }
  }

  return { candidate_prune: candidatePrune, auto_prune: autoPrune, merge_suggestions: mergeSuggestions };
}

/**
 * 执行自动修剪。
 *
 * @param {Object} capabilityTree - CapabilityTree 实例
 * @param {boolean} [aggressive=false] - 是否执行严格修剪（含 candidate_prune）
 * @returns {{ pruned: number, auto_pruned: string[], candidate_pruned: string[] }}
 */
function pruneTree(capabilityTree, aggressive = false) {
  const allNodes = Object.values(capabilityTree.data.nodes || {});
  const { candidate_prune, auto_prune } = analyzePruning(allNodes);

  const autoPruned = [];
  const candidatePruned = [];

  for (const node of auto_prune) {
    if (capabilityTree.removeNode(node.id)) {
      autoPruned.push(node.id);
    }
  }

  if (aggressive) {
    for (const node of candidate_prune) {
      if (capabilityTree.removeNode(node.id)) {
        candidatePruned.push(node.id);
      }
    }
  }

  return {
    pruned: autoPruned.length + candidatePruned.length,
    auto_pruned: autoPruned,
    candidate_pruned: candidatePruned,
  };
}

module.exports = { analyzePruning, pruneTree, jaccardSimilarity, tokenize };
