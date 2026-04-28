'use strict';

/**
 * Gene 数据结构与 CRUD (xm-auto-evo 版)
 * 
 * 移植自 xm-evo/src/gep/gene.js
 * 
 * Gene 是进化的基本单元，描述一种可执行的变异策略。
 */

const crypto = require('node:crypto');

/**
 * 创建新 Gene
 */
function createGene(params) {
  if (!params.category || !['repair', 'optimize', 'innovate'].includes(params.category)) {
    throw new Error(`Invalid gene category: ${params.category}`);
  }
  if (!Array.isArray(params.signals_match) || params.signals_match.length === 0) {
    throw new Error('Gene must have at least one signal_match');
  }
  if (!Array.isArray(params.strategy) || params.strategy.length === 0) {
    throw new Error('Gene must have at least one strategy step');
  }

  return {
    type: 'Gene',
    id: params.id || `gene_${crypto.randomUUID().slice(0, 8)}`,
    category: params.category,
    signals_match: params.signals_match,
    preconditions: params.preconditions || [],
    strategy: params.strategy,
    constraints: {
      max_files: params.constraints?.max_files ?? 12,
      forbidden_paths: params.constraints?.forbidden_paths ?? ['.git', 'node_modules'],
    },
    validation: params.validation || [],
    capability_node_id: params.capability_node_id || null,
    v_score: params.v_score ?? null,
    created_at: new Date().toISOString(),
  };
}

/**
 * 更新 Gene 字段
 */
function updateGene(gene, updates) {
  const allowed = ['category', 'signals_match', 'preconditions', 'strategy', 'constraints', 'validation', 'capability_node_id', 'v_score'];
  const result = { ...gene };
  for (const key of allowed) {
    if (updates[key] !== undefined) result[key] = updates[key];
  }
  return result;
}

/**
 * 计算 Gene 对信号的匹配度分数
 */
function matchScore(gene, signals) {
  if (!gene.signals_match || gene.signals_match.length === 0) return 0;
  if (!Array.isArray(signals) || signals.length === 0) return 0;
  
  const matched = gene.signals_match.filter(sig => signals.includes(sig));
  return matched.length / gene.signals_match.length;
}

module.exports = { createGene, updateGene, matchScore };
