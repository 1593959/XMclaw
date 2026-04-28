'use strict';

/**
 * 测试能力生成 - 直接创建 Gene 和 Skill
 */

const { createGene } = require('../gep/gene');
const { addGene } = require('../gep/store');
const { addCapsule } = require('../gep/store');
const { createEvent, appendEvent } = require('../gep/event');
const { autoCreateSkill } = require('./skill_maker');

// 创建一个测试 Gene
const testGene = createGene({
  id: 'gene_test_proactive_retrieval',
  category: 'optimize',
  signals_match: [
    'intent:search',
    'search_topic',
    'capability_candidate:memory_retrieval',
    'tool_issue:search'
  ],
  preconditions: [],
  strategy: [
    '检测用户搜索需求',
    '检查 mmx search 可用性',
    '使用 memory_search 检索记忆',
    '执行搜索',
    '更新记忆'
  ],
  constraints: {
    max_files: 1,
    forbidden_paths: ['.git', 'node_modules']
  },
  validation: [],
  capability_node_id: null,
  v_score: 85,
  created_at: new Date().toISOString()
});

addGene(testGene);
console.log('✅ Gene 创建:', testGene.id);

// 创建胶囊（模拟固化）
const capsule = {
  type: 'Capsule',
  id: `capsule_${Date.now()}`,
  gene_id: testGene.id,
  mutation_category: 'optimize',
  signals: testGene.signals_match,
  files_changed: [],
  summary: '主动检索能力胶囊',
  metrics: { blast_files: 0, blast_lines: 0, validation_passed: true },
  created_at: new Date().toISOString()
};
addCapsule(capsule);
console.log('✅ Capsule 创建:', capsule.id);

// 创建技能
const pattern = {
  category: 'proactive_retrieval',
  signature: 'intent:search',
  confidence: 0.9,
  example: '用户询问知识性问题时自动搜索'
};
const skill = autoCreateSkill(pattern);
console.log('✅ Skill 创建:', skill?.skillId || skill?.existed ? '已存在' : '失败');

// 记录事件
appendEvent(createEvent({
  event_type: 'solidify_success',
  payload: { gene_id: testGene.id, capsule_id: capsule.id, category: 'test' }
}));

console.log('\n🎉 测试完成!');
console.log('Gene:', testGene.id);
console.log('Capsule:', capsule.id);
console.log('Skill:', skill?.skillId || (skill?.existed ? '已存在' : '失败'));
