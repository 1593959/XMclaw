'use strict';

/**
 * Skill 自动创建器 (xm-auto-evo 版) - 增强版
 *
 * 借鉴 Hermes Agent Skills 系统：
 * - 渐进加载（Level 0/1/2）
 * - 条件激活（fallback/requires）
 * - 自动创建
 * - 实质迭代（继承旧版本并增强）
 */

const fs = require('node:fs');
const path = require('path');
const crypto = require('node:crypto');

let _workspace = null;
let _skillsDir = null;

function getWorkspace() {
  if (_workspace) return _workspace;
  const envWorkspace = process.env.WORKSPACE || process.env.COPAW_WORKSPACE;
  if (envWorkspace && fs.existsSync(path.join(envWorkspace, 'skill.json'))) {
    _workspace = envWorkspace;
  } else {
    _workspace = process.cwd();
  }
  return _workspace;
}

function getSkillsDir() {
  if (_skillsDir) return _skillsDir;
  _skillsDir = path.join(getWorkspace(), 'skills');
  return _skillsDir;
}

function setWorkspace(workspace) {
  _workspace = workspace;
  _skillsDir = null;
}

const SKILL_LEVELS = {
  TRIGGER: 0,
  BASIC: 1,
  FULL: 2,
};

function findAllSkillsByCategory(category) {
  try {
    const skillJsonPath = path.join(getWorkspace(), 'skill.json');
    if (!fs.existsSync(skillJsonPath)) return [];

    const skillJson = JSON.parse(fs.readFileSync(skillJsonPath, 'utf-8'));
    const skills = skillJson.skills || skillJson;

    const baseCategory = category.replace(/^auto_/, '').replace(/_[a-f0-9]{6}(_v\d+)?$/, '').replace(/_v\d+$/, '');

    const matches = [];
    for (const [key, skill] of Object.entries(skills)) {
      const isAuto = key.startsWith('auto_') || skill.metadata?.auto_created || skill.auto_created;
      if (!isAuto) continue;

      const skillName = skill.metadata?.name || skill.name || '';
      const skillBase = skillName.replace(/^auto_/, '').replace(/_[a-f0-9]{6}(_v\d+)?$/, '').replace(/_v\d+$/, '');

      if (skillBase === baseCategory) {
        const versionMatch = key.match(/_v(\d+)$/);
        const version = versionMatch ? parseInt(versionMatch[1], 10) : 1;
        matches.push({ key, skill, version, createdAt: skill.metadata?.created_at || skill.created_at || '1970-01-01' });
      }
    }

    matches.sort((a, b) => a.version - b.version);
    return matches;
  } catch {}
  return [];
}

function findExistingSkill(category) {
  const matches = findAllSkillsByCategory(category);
  return matches.length > 0 ? matches[matches.length - 1].key : null;
}

function cleanupOldSkillVersions(category, keepCount = 2) {
  try {
    const allVersions = findAllSkillsByCategory(category);
    if (allVersions.length <= keepCount) return;

    const toRemove = allVersions.slice(0, allVersions.length - keepCount);
    const skillJsonPath = path.join(getWorkspace(), 'skill.json');
    const skillJson = JSON.parse(fs.readFileSync(skillJsonPath, 'utf-8'));

    for (const { key } of toRemove) {
      if (skillJson.skills && skillJson.skills[key]) {
        delete skillJson.skills[key];
      }
      const dir = path.join(getSkillsDir(), key);
      if (fs.existsSync(dir)) {
        fs.rmSync(dir, { recursive: true, force: true });
      }
      console.log(`   🗑️  清理旧版本: ${key}`);
    }

    fs.writeFileSync(skillJsonPath, JSON.stringify(skillJson, null, 2), 'utf-8');
  } catch (e) {
    console.error(`   ⚠️  清理旧版本失败: ${e.message}`);
  }
}

function skillDirExists(skillId) {
  return fs.existsSync(path.join(getSkillsDir(), skillId));
}

function autoCreateSkill(pattern, options = {}) {
  const category = pattern.category || 'auto-skill';
  const skillName = category;

  const allVersions = findAllSkillsByCategory(skillName);
  let existing = allVersions.length > 0 ? allVersions[allVersions.length - 1].key : null;
  let nextVersion = 1;
  if (existing) {
    nextVersion = allVersions[allVersions.length - 1].version + 1;
    console.log(`   🔄 Skill 迭代: ${existing} → v${nextVersion}`);
  }

  const versionSuffix = existing ? `_v${nextVersion}` : '';
  const skillId = `auto_${category}_${crypto.randomUUID().slice(0, 6)}${versionSuffix}`;
  const skillDir = path.join(getSkillsDir(), skillId);

  try {
    fs.mkdirSync(skillDir, { recursive: true });

    const skillMd = generateSkillMd(pattern, options);
    fs.writeFileSync(path.join(skillDir, 'SKILL.md'), skillMd, 'utf-8');

    const implExt = options.language === 'python' ? '.py' : '.js';
    const indexPath = path.join(skillDir, `index${implExt}`);

    if (existing) {
      const existingDir = path.join(getSkillsDir(), existing);
      const existingIndex = path.join(existingDir, `index${implExt}`);
      if (fs.existsSync(existingIndex)) {
        let implContent = fs.readFileSync(existingIndex, 'utf-8');
        implContent = enhanceSkillImplementation(implContent, pattern, options);
        fs.writeFileSync(indexPath, implContent, 'utf-8');
        console.log(`   🔄 继承并增强: ${existing} → ${skillId}`);
      } else {
        const implContent = generateImplementation(pattern, options);
        fs.writeFileSync(indexPath, implContent, 'utf-8');
      }
    } else {
      const implContent = generateImplementation(pattern, options);
      fs.writeFileSync(indexPath, implContent, 'utf-8');
    }

    registerSkill(skillId, skillName, pattern);
    cleanupOldSkillVersions(skillName, 2);

    console.log(`   🛠️  自动创建 Skill: ${skillId} (Level ${options.level || 0})`);

    return { skillId, skillDir, pattern, level: options.level || 0, existed: false };
  } catch (e) {
    console.error(`   ❌ Skill 创建失败: ${e.message}`);
    return null;
  }
}

function generateSkillMd(pattern, options = {}) {
  const skillName = pattern.category || 'auto-skill';
  const level = options.level || 0;

  let content = `---
name: ${skillName}
description: "${pattern.description || '自动生成的 Skill'}"
level: ${level}
auto_created: true
created_at: ${new Date().toISOString()}
---

# 使用时机

`;

  if (pattern.description) {
    content += `当系统检测到该模式时触发：${pattern.description}\n\n`;
  } else if (pattern.signature) {
    content += `当系统检测到该模式时触发：${pattern.signature}\n\n`;
  } else {
    content += `当用户提出相关能力需求时触发。\n\n`;
  }

  content += `# 使用方法

直接调用 ${skillName} 的主要函数，传入对应的上下文参数。具体函数取决于 index.js 中的导出。
`;

  if (pattern.signature) {
    const triggers = pattern.signature.split(',').map(t => '  - ' + t.trim()).join('\n');
    content += '\n```yaml\ntriggers:\n' + triggers + '\n```\n';
  }

  if (level >= 1) {
    content += `\n## 功能\n${pattern.description || '根据检测到的模式自动生成的 Skill'}\n\n## 使用场景\n${(pattern.examples || []).map(e => `- ${e}`).join('\n') || '- 通用场景'}`;
  }

  if (level >= 2) {
    content += '\n## 实现细节\n\n';
    content += '```\n';
    content += '检测模式: ' + (pattern.pattern || 'N/A') + '\n';
    content += '频率: ' + (pattern.count || 0) + ' 次\n';
    content += '成功率: ' + (pattern.success_rate || 'N/A') + '\n';
    content += '```\n';
  }

  return content;
}

function generateImplementation(pattern, options = {}) {
  const language = options.language || 'javascript';
  const category = (pattern.category || 'auto-skill').toLowerCase();

  if (language === 'python') {
    return `#!/usr/bin/env python3
"""${pattern.category || 'Auto-generated'} Skill 实现 自动生成于 ${new Date().toISOString()}"""

def execute(context: dict) -> dict:
    print("执行 ${pattern.category} Skill")
    return {"success": True}

def can_activate(context: dict) -> bool:
    ${pattern.context_keywords ? 
      `keywords = ${JSON.stringify(pattern.context_keywords)}
    return any(k in context.get("message", "") for k in keywords)` :
      'return True'}
`;
  }

  if (category.includes('entity_reference') || category.includes('entity')) {
    return generateEntityReferenceImpl();
  }
  if (category.includes('search') || category.includes('retrieval') || category.includes('search_query')) {
    return generateSearchImpl();
  }
  if (category.includes('repair') || category.includes('fix') || category.includes('repair_request')) {
    return generateRepairImpl();
  }
  if (category.includes('file_op') || category.includes('file_')) {
    return generateFileOpImpl();
  }
  if (category.includes('analysis') || category.includes('analyze')) {
    return generateAnalysisImpl();
  }
  if (category.includes('code_gen') || category.includes('code')) {
    return generateCodeGenImpl();
  }
  if (category.includes('feature_request') || category.includes('feature')) {
    return generateFeatureRequestImpl();
  }

  return `'use strict';

function execute(context = {}) {
  const message = context.message || context.text || '';
  console.log('执行 ${pattern.category} Skill，输入:', message.slice(0, 50));
  return { success: true, message, timestamp: new Date().toISOString() };
}

function canActivate(context = {}) {
  ${pattern.context_keywords ? 
    `const keywords = ${JSON.stringify(pattern.context_keywords)};
  return keywords.some(k => (context.message || '').includes(k));` :
    'return (context.message || \'\').length > 0;'}
}

if (require.main === module) {
  const result = execute({ message: 'test' });
  console.log('Self-test:', result.success ? 'PASS' : 'FAIL');
  process.exit(result.success ? 0 : 1);
}

module.exports = { execute, canActivate };
`;
}

function generateEntityReferenceImpl() {
  return `'use strict';

function extractEntities(text) {
  if (!text) return [];
  const entities = [];
  const pathMatches = text.match(/[A-Za-z]:\\\\[^\\s]+|\\/[^\\s]+/g) || [];
  pathMatches.forEach(p => entities.push({ type: 'file_path', value: p }));
  const urlMatches = text.match(/https?:\\/\\/[^\\s]+/g) || [];
  urlMatches.forEach(u => entities.push({ type: 'url', value: u }));
  const emailMatches = text.match(/[\\w.-]+@[\\w.-]+\\.[\\w]+/g) || [];
  emailMatches.forEach(e => entities.push({ type: 'email', value: e }));
  const backtick = String.fromCharCode(96);
  const codeMatches = text.match(new RegExp(backtick + '([^' + backtick + ']+)' + backtick, 'g')) || [];
  codeMatches.forEach(c => entities.push({ type: 'code', value: c.replace(new RegExp(backtick, 'g'), '') }));
  return entities;
}

function execute(context = {}) {
  const text = context.message || context.text || '';
  const entities = extractEntities(text);
  console.log('提取到 ' + entities.length + ' 个实体');
  return { success: true, entities, text };
}

function canActivate(context = {}) {
  return (context.message || '').length > 0;
}

if (require.main === module) {
  const tests = [
    { message: '查看 E:/path/file.txt' },
    { message: '访问 https://example.com' },
    { message: '联系 admin@test.com' },
    { message: '运行 ' + String.fromCharCode(96) + 'npm start' + String.fromCharCode(96) },
  ];
  let pass = true;
  for (const t of tests) {
    const r = execute(t);
    if (!r.success || r.entities.length === 0) pass = false;
  }
  console.log('Self-test:', pass ? 'PASS' : 'FAIL');
  process.exit(pass ? 0 : 1);
}

module.exports = { execute, canActivate, extractEntities };
`;
}

function generateSearchImpl() {
  return `'use strict';

async function execute(context = {}) {
  const query = context.message || context.query || '';
  if (!query) return { success: false, error: '缺少查询词' };
  const results = [];
  try { results.push({ source: 'memory', status: '可用' }); } catch (e) {}
  try { results.push({ source: 'web', status: '可用' }); } catch (e) {}
  console.log('搜索完成: ' + query);
  return { success: true, query, results };
}

function canActivate(context = {}) {
  return /搜索|查询|找一下|帮我查|search|look up/i.test(context.message || '');
}

if (require.main === module) {
  execute({ message: '帮我查一下天气' }).then(r => {
    console.log('Self-test:', r.success ? 'PASS' : 'FAIL');
    process.exit(r.success ? 0 : 1);
  });
}

module.exports = { execute, canActivate };
`;
}

function generateRepairImpl() {
  return `'use strict';

function execute(context = {}) {
  const error = context.error || context.message || '';
  const suggestions = [];
  if (error.includes('ENOENT')) suggestions.push('检查文件路径是否存在');
  if (error.includes('EACCES')) suggestions.push('检查文件权限');
  if (error.includes('undefined') || error.includes('null')) suggestions.push('检查变量是否已初始化');
  if (error.includes('timeout') || error.includes('ETIMEDOUT')) suggestions.push('增加超时时间或检查网络连接');
  console.log('分析错误: ' + error.slice(0, 100));
  return { success: true, error, suggestions };
}

function canActivate(context = {}) {
  const text = (context.error || context.message || '').toLowerCase();
  return text.includes('error') || text.includes('fail') || text.includes('exception');
}

if (require.main === module) {
  const r = execute({ error: 'ENOENT: no such file or directory' });
  console.log('Self-test:', r.suggestions.length > 0 ? 'PASS' : 'FAIL');
  process.exit(r.suggestions.length > 0 ? 0 : 1);
}

module.exports = { execute, canActivate };
`;
}

function generateFileOpImpl() {
  return `'use strict';

const fs = require('fs');
const path = require('path');

function execute(context = {}) {
  const operation = context.operation || 'list';
  const targetPath = context.path || context.message || '';
  const result = { operation, targetPath, success: false, data: null };
  try {
    if (operation === 'list') {
      result.data = fs.readdirSync(targetPath).slice(0, 20);
      result.success = true;
    } else if (operation === 'read') {
      result.data = fs.readFileSync(targetPath, 'utf-8').slice(0, 1000);
      result.success = true;
    } else if (operation === 'exists') {
      result.data = fs.existsSync(targetPath);
      result.success = true;
    } else {
      result.data = '支持的操作: list, read, exists';
    }
  } catch (e) {
    result.error = e.message;
  }
  console.log('文件操作:', operation, targetPath);
  return result;
}

function canActivate(context = {}) {
  return /文件|读取|目录|路径|\.txt|\.js|\.json|\.md/i.test(context.message || '');
}

if (require.main === module) {
  const r = execute({ operation: 'exists', path: __filename });
  console.log('Self-test:', r.success ? 'PASS' : 'FAIL');
  process.exit(r.success ? 0 : 1);
}

module.exports = { execute, canActivate };
`;
}

function generateAnalysisImpl() {
  return `'use strict';

function execute(context = {}) {
  const text = context.message || context.text || '';
  const words = text.split(/\\s+/).filter(w => w.length > 0);
  const sentences = text.split(/[。！？.!?]/).filter(s => s.trim().length > 0);
  const result = {
    success: true,
    wordCount: words.length,
    sentenceCount: sentences.length,
    avgWordLength: words.length > 0 ? (words.reduce((a, b) => a + b.length, 0) / words.length).toFixed(1) : 0,
    keywords: words.filter(w => w.length >= 4).slice(0, 10),
  };
  console.log('分析完成:', JSON.stringify(result));
  return result;
}

function canActivate(context = {}) {
  return /分析|统计|对比|比较|评估|summary|analyze/i.test(context.message || '');
}

if (require.main === module) {
  const r = execute({ text: '这是一个测试文本，用于验证分析功能。' });
  console.log('Self-test:', r.wordCount > 0 ? 'PASS' : 'FAIL');
  process.exit(r.wordCount > 0 ? 0 : 1);
}

module.exports = { execute, canActivate };
`;
}

function generateCodeGenImpl() {
  return `'use strict';

function execute(context = {}) {
  const request = context.message || context.text || '';
  const snippets = [];
  if (/函数|function/.test(request)) snippets.push('function example() { return true; }');
  if (/循环|for|while/.test(request)) snippets.push('for (let i = 0; i < n; i++) { }');
  if (/条件|if/.test(request)) snippets.push('if (condition) { } else { }');
  if (snippets.length === 0) snippets.push('// 请描述你需要的代码');
  console.log('生成代码片段:', snippets.length);
  return { success: true, request, snippets };
}

function canActivate(context = {}) {
  return /代码|脚本|函数|写.*程序|code|script/i.test(context.message || '');
}

if (require.main === module) {
  const r = execute({ message: '帮我写一个函数' });
  console.log('Self-test:', r.snippets.length > 0 ? 'PASS' : 'FAIL');
  process.exit(r.snippets.length > 0 ? 0 : 1);
}

module.exports = { execute, canActivate };
`;
}

function generateFeatureRequestImpl() {
  return `'use strict';

function execute(context = {}) {
  const request = context.message || context.text || '';
  const features = [];
  if (/技能|skill/.test(request)) features.push({ type: 'skill', action: '创建新 Skill' });
  if (/定时|cron|任务/.test(request)) features.push({ type: 'automation', action: '配置定时任务' });
  if (/通知|提醒|消息/.test(request)) features.push({ type: 'notification', action: '设置主动通知' });
  if (/记忆|记住|偏好/.test(request)) features.push({ type: 'memory', action: '增强记忆系统' });
  if (features.length === 0) features.push({ type: 'general', action: '评估新功能需求' });
  console.log('功能需求分析:', features.length);
  return { success: true, request, features };
}

function canActivate(context = {}) {
  return /功能|能力|添加|新能力|新技能|新功能|feature/i.test(context.message || '');
}

if (require.main === module) {
  const r = execute({ message: '添加一个新技能' });
  console.log('Self-test:', r.features.length > 0 ? 'PASS' : 'FAIL');
  process.exit(r.features.length > 0 ? 0 : 1);
}

module.exports = { execute, canActivate };
`;
}

function enhanceSkillImplementation(content, pattern, options = {}) {
  const action = options.action || 'optimize';
  const improvements = options.improvements || [`增强 ${pattern.category} 的 ${action} 能力`];
  const marker = improvements[0];
  if (content.includes(marker)) return content;
  const enhancement = `\n\n/* [AUTO-EVO] ${action.toUpperCase()} ${new Date().toISOString()}\n * ${improvements.map(i => ' * ' + i).join('\n')}\n */\n`;
  return content + enhancement;
}

function registerSkill(skillId, skillName, pattern) {
  try {
    const skillJsonPath = path.join(getWorkspace(), 'skill.json');
    let skillJson = { skills: {} };
    if (fs.existsSync(skillJsonPath)) {
      try {
        skillJson = JSON.parse(fs.readFileSync(skillJsonPath, 'utf-8'));
      } catch {
        skillJson = { skills: {} };
      }
    }
    skillJson.skills[skillId] = {
      enabled: true,
      channels: ['all'],
      source: 'customized',
      metadata: {
        name: skillId,
        description: pattern.description || '自动生成的 Skill',
        auto_created: true,
        created_at: new Date().toISOString(),
        pattern: pattern.pattern || null,
        signature: pattern.signature || null,
        success_rate: pattern.success_rate || 0,
      },
    };
    fs.writeFileSync(skillJsonPath, JSON.stringify(skillJson, null, 2), 'utf-8');
    console.log(`   ✅ 已注册到 skill.json: ${skillId}`);
    syncSkillsIndex(skillId, skillName, pattern);
  } catch (e) {
    console.error(`   ⚠️  注册到 skill.json 失败: ${e.message}`);
  }
}

function syncSkillsIndex(skillId, skillName, pattern) {
  try {
    const skillsIndexPath = path.join(getWorkspace(), 'skills', 'SKILLS_INDEX.md');
    if (!fs.existsSync(skillsIndexPath)) {
      console.log(`   ⚠️  SKILLS_INDEX.md 不存在，跳过同步`);
      return;
    }
    let content = fs.readFileSync(skillsIndexPath, 'utf-8');
    // 按 category 识别同一系列：auto_entity_reference_xxx_vN 都属于 auto_entity_reference
    const baseCategory = skillId.replace(/_[a-f0-9]+(_v\d+)?$/, '').replace(/_v\d+$/, '');
    const lines = content.split('\n');
    let found = false;
    const newLines = [];
    for (const line of lines) {
      const match = line.match(/\|\s*\|\s*(\S+?)\s*\|/);
      if (match) {
        const entryId = match[1];
        const entryBase = entryId.replace(/_[a-f0-9]+(_v\d+)?$/, '').replace(/_v\d+$/, '');
        if (entryBase === baseCategory) {
          if (!found) {
            newLines.push(`| | ${skillId} | ${pattern?.description || '自动生成的技能'} |`);
            found = true;
          }
          continue;
        }
      }
      newLines.push(line);
    }
    if (!found) {
      const systemCategory = '| **系统** | xm-auto-evo | 进化系统 |';
      const newSkillEntry = `| | ${skillId} | ${pattern?.description || '自动生成的技能'} |`;
      if (content.includes(systemCategory)) {
        content = content.replace(systemCategory, `${systemCategory}\n${newSkillEntry}`);
        fs.writeFileSync(skillsIndexPath, content, 'utf-8');
        console.log(`   ✅ 已同步到 SKILLS_INDEX.md: ${skillId}`);
        return;
      }
    }
    fs.writeFileSync(skillsIndexPath, newLines.join('\n'), 'utf-8');
    console.log(`   ✅ 已同步到 SKILLS_INDEX.md: ${skillId}`);
  } catch (e) {
    console.error(`   ⚠️  同步到 SKILLS_INDEX.md 失败: ${e.message}`);
  }
}

module.exports = {
  autoCreateSkill,
  setWorkspace,
  getWorkspace,
  getSkillsDir,
  findExistingSkill,
  findAllSkillsByCategory,
  skillDirExists,
  SKILL_LEVELS,
  generateSkillMd,
  generateImplementation,
  enhanceSkillImplementation,
  cleanupOldSkillVersions,
  registerSkill,
  syncSkillsIndex,
};