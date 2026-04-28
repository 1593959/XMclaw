'use strict';

/**
 * 主动检索 Gene 生成器
 * 
 * 根据用户行为模式生成主动检索能力。
 */

const { createGene } = require('../gep/gene');
const { addGene } = require('../gep/store');

/**
 * 为主动检索能力创建 Gene
 */
function createProactiveRetrievalGene() {
  const gene = createGene({
    id: 'gene_proactive_retrieval',
    category: 'optimize',
    signals_match: [
      'intent:search',
      'search_topic',
      'capability_candidate:memory_retrieval',
      'capability_candidate:web_search',
      'tool_issue:search'
    ],
    preconditions: [
      { type: 'tool_available', name: 'mmx' },
      { type: 'tool_available', name: 'memory_search' }
    ],
    strategy: [
      '检测用户知识性提问或搜索需求',
      '先检查 mmx search 是否可用',
      '使用 memory_search 检索相关记忆',
      '如无记忆或需要最新信息，使用搜索工具',
      '搜索后主动更新记忆'
    ],
    constraints: {
      max_files: 1,
      forbidden_paths: ['.git', 'node_modules', 'config/'],
      timeout_ms: 30000
    },
    validation: [],
    capability_node_id: 'cap_proactive_retrieval',
    v_score: 85,  // 高价值技能
    created_at: new Date().toISOString()
  });

  addGene(gene);
  console.log('✅ 创建主动检索 Gene: gene_proactive_retrieval');
  return gene;
}

// 如果直接运行此脚本，创建 Gene
if (require.main === module) {
  createProactiveRetrievalGene();
}

module.exports = { createProactiveRetrievalGene };
