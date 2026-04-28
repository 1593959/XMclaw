'use strict';

/**
 * Gene 自动生成器 (xm-auto-evo 版) - 增强版
 *
 * 根据检测到的模式自动创建新 Gene，并附带可执行的代码修改指令。
 */

const crypto = require('node:crypto');
const { createGene } = require('../gep/gene');
const { addGene } = require('../gep/store');
const { CapabilityTree } = require('./tree');

/**
 * 根据模式自动生成 Gene
 * @param {Object} pattern - 检测到的模式 { category, signature, example, confidence, description }
 * @param {Object} config - 配置
 * @returns {Object} 创建的 Gene
 */
function autoGenerateGene(pattern, config = {}) {
  // 将模式类别映射到有效的 Gene 类别
  const validCategories = ['repair', 'optimize', 'innovate'];
  let category = pattern.category || 'optimize';
  if (!validCategories.includes(category)) {
    category = (pattern.confidence || 0.5) > 0.8 ? 'optimize' : 'repair';
  }

  const signal = pattern.signature || pattern.category || 'unknown';
  const geneId = `auto_${crypto.randomUUID().slice(0, 8)}`;

  // 根据 category 生成可执行策略
  const strategy = generateStrategy(category, signal, pattern);

  // 生成要修改的文件和验证命令
  const { files_to_modify, validation } = generateCodePlan(category, signal, pattern, geneId);

  const gene = createGene({
    id: geneId,
    category,
    signals_match: [signal, `high_tool_usage:${signal}`, `capability_candidate:${signal}`],
    preconditions: [],
    strategy,
    files_to_modify,
    constraints: {
      max_files: 5,
      forbidden_paths: ['.git', 'node_modules', 'config/'],
    },
    validation,
    capability_node_id: null,
    v_score: null,
  });

  // 扩展 gene 字段（createGene 不认识的字段需要手动加）
  gene.files_to_modify = files_to_modify;

  addGene(gene);
  console.log(`   ✅ 自动生成 Gene: ${gene.id} (${category})`);
  return gene;
}

/**
 * 生成策略步骤
 */
function generateStrategy(category, signal, pattern) {
  const desc = pattern.description || pattern.signature || '自动检测到的模式';

  if (category === 'repair') {
    return [
      `识别问题: ${desc}`,
      `定位相关 Skill 或源码文件`,
      `修复缺陷并添加边界处理`,
      `运行验证命令确认修复有效`,
    ];
  }

  if (category === 'optimize') {
    return [
      `分析现有实现: ${desc}`,
      `找出性能或体验瓶颈`,
      `优化代码逻辑或增加缓存`,
      `运行验证命令确认优化未破坏功能`,
    ];
  }

  // innovate
  return [
    `识别新需求: ${desc}`,
    `设计最小可行实现 (MVP)`,
    `创建或修改 Skill 文件`,
    `编写验证用例并运行测试`,
  ];
}

/**
 * 生成代码修改计划
 */
function generateCodePlan(category, signal, pattern, geneId) {
  const files_to_modify = [];
  const validation = [];

  // 如果信号指向一个明确的 Skill category，就改进对应的 Skill
  const skillCategory = signal.replace(/^auto_/, '').replace(/:.*$/, '').replace(/\s+/g, '_').toLowerCase();

  if (skillCategory && skillCategory !== 'unknown') {
    files_to_modify.push({
      type: 'skill',
      target: skillCategory,
      action: category,
      improvements: [
        `增强 ${skillCategory} 的 ${category} 能力`,
        `根据模式 "${pattern.signature || ''}" 添加对应处理逻辑`,
      ],
    });

    // 验证：查找最新版本的 Skill 目录并运行 node index.js
    validation.push(`node -e "const fs=require('fs'); const path=require('path'); const d='skills'; if(!fs.existsSync(d)){console.error('skills dir missing');process.exit(1)} const dirs=fs.readdirSync(d).filter(x=>x.startsWith('auto_${skillCategory}_')\&\&fs.statSync(path.join(d,x)).isDirectory()).sort(); if(dirs.length===0){console.error('No skill dir found');process.exit(1)} const latest=dirs[dirs.length-1]; const idx=path.join(d,latest,'index.js'); if(!fs.existsSync(idx)){console.error('No index.js in skill');process.exit(1)} console.log('Skill found:', latest)"`);
    validation.push(`node skills/auto_${skillCategory}_*/index.js`);
  } else {
    // 通用：改进引擎自身
    files_to_modify.push({
      type: 'engine',
      target: 'engine.js',
      action: category,
      improvements: [`根据模式优化引擎 ${category} 逻辑`],
    });
  }

  return { files_to_modify, validation };
}

/**
 * 根据趋势自动创建能力节点
 */
function autoCreateCapabilityNode(pattern, tree) {
  if (!tree) tree = new CapabilityTree();
  const nodeId = `cap_auto_${crypto.randomUUID().slice(0, 8)}`;
  tree.addNode({
    id: nodeId,
    name: pattern.signature || pattern.category || 'Auto Capability',
    level: 'mid',
    parent_id: 'cap',
    children: [],
    status: 'active',
    preconditions: [],
    linked_genes: [],
    linked_skills: [],
    input: '',
    output: '',
    failure_boundary: '',
    category: pattern.category,
    confidence: pattern.confidence || 0.5,
    auto_generated: true,
    created_at: new Date().toISOString(),
    last_used: null,
  });
  console.log(`   🌳 自动创建能力节点: ${nodeId}`);
  return nodeId;
}

module.exports = { autoGenerateGene, autoCreateCapabilityNode, generateStrategy, generateCodePlan };
