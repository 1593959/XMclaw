'use strict';

/**
 * 能力节点 (Capability Node) 数据结构与操作。
 *
 * 移植自 xm-evo/src/tree/node.js
 */

const VALID_LEVELS = ['low', 'mid', 'high'];
const VALID_STATUSES = ['active', 'candidate', 'pruned'];

function createNode(params) {
  if (!params.id) throw new Error('Node id is required');
  if (!params.name) throw new Error('Node name is required');
  if (!VALID_LEVELS.includes(params.level)) throw new Error(`Invalid level: ${params.level}`);
  if (params.parent_id === undefined || params.parent_id === '') throw new Error('Node parent_id is required');

  return {
    id: params.id,
    name: params.name,
    level: params.level,
    parent_id: params.parent_id,
    input: params.input || '',
    output: params.output || '',
    preconditions: Array.isArray(params.preconditions) ? [...params.preconditions] : [],
    failure_boundary: params.failure_boundary || '',
    linked_genes: Array.isArray(params.linked_genes) ? [...params.linked_genes] : [],
    linked_skills: Array.isArray(params.linked_skills) ? [...params.linked_skills] : [],
    children: [],
    status: 'active',
    v_score: null,
    last_triggered: null,
    trigger_count: 0,
  };
}

function validateNode(node) {
  const errors = [];
  if (!node.id) errors.push('id is required');
  if (!node.name) errors.push('name is required');
  if (!VALID_LEVELS.includes(node.level)) errors.push(`invalid level: ${node.level}`);
  if (node.parent_id === undefined || node.parent_id === '') errors.push('parent_id is required');
  if (!VALID_STATUSES.includes(node.status)) errors.push(`invalid status: ${node.status}`);
  if (!Array.isArray(node.linked_genes)) errors.push('linked_genes must be an array');
  if (!Array.isArray(node.linked_skills)) errors.push('linked_skills must be an array');
  if (!Array.isArray(node.preconditions)) errors.push('preconditions must be an array');
  if (!Array.isArray(node.children)) errors.push('children must be an array');
  return { valid: errors.length === 0, errors };
}

function touchNode(node) {
  return {
    ...node,
    trigger_count: node.trigger_count + 1,
    last_triggered: new Date().toISOString(),
  };
}

module.exports = { createNode, validateNode, touchNode, VALID_LEVELS, VALID_STATUSES };
