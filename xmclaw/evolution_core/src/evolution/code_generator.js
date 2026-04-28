'use strict';

/**
 * 代码生成器 (Code Generator)
 *
 * 根据 Gene 的 files_to_modify 指令，自动修改 Skill 文件。
 */

const fs = require('node:fs');
const path = require('node:path');
const { generateImplementation, enhanceSkillImplementation: enhanceImpl } = require('./skill_maker');

function executeCodePlan(gene, workspace) {
  const filesToModify = gene.files_to_modify || [];
  if (filesToModify.length === 0) {
    return { success: true, changedFiles: [], newFiles: [] };
  }

  const changedFiles = [];
  const newFiles = [];

  for (const plan of filesToModify) {
    if (plan.type === 'skill') {
      const result = modifySkill(plan, workspace);
      if (!result.success) {
        return { success: false, changedFiles, newFiles, error: result.error };
      }
      changedFiles.push(...result.changedFiles);
      newFiles.push(...result.newFiles);
    }
  }

  return { success: true, changedFiles, newFiles };
}

function modifySkill(plan, workspace) {
  const changedFiles = [];
  const newFiles = [];
  const skillDirs = findSkillDirs(plan.target, workspace);

  if (skillDirs.length === 0) {
    return createNewSkill(plan, workspace);
  }

  const latestDir = skillDirs[skillDirs.length - 1];
  const skillPath = path.join(workspace, 'skills', latestDir);
  const indexPath = path.join(skillPath, 'index.js');

  if (!fs.existsSync(indexPath)) {
    const impl = generateImplementation({ category: plan.target });
    fs.writeFileSync(indexPath, impl, 'utf-8');
    newFiles.push(indexPath);
  } else {
    let content = fs.readFileSync(indexPath, 'utf-8');
    content = enhanceImpl(content, { category: plan.target }, { action: plan.action, improvements: plan.improvements });
    fs.writeFileSync(indexPath, content, 'utf-8');
    changedFiles.push(indexPath);
  }

  const skillMdPath = path.join(skillPath, 'SKILL.md');
  if (fs.existsSync(skillMdPath)) {
    const updated = enhanceSkillMd(skillMdPath, plan.improvements);
    fs.writeFileSync(skillMdPath, updated, 'utf-8');
    changedFiles.push(skillMdPath);
  }

  return { success: true, changedFiles, newFiles };
}

function findSkillDirs(category, workspace) {
  const skillsDir = path.join(workspace, 'skills');
  if (!fs.existsSync(skillsDir)) return [];

  const dirs = fs.readdirSync(skillsDir).filter(dir => {
    const full = path.join(skillsDir, dir);
    return fs.statSync(full).isDirectory() && dir.includes(category);
  });

  dirs.sort((a, b) => {
    const va = (a.match(/_v(\d+)$/) || [0, 0])[1];
    const vb = (b.match(/_v(\d+)$/) || [0, 0])[1];
    return parseInt(va, 10) - parseInt(vb, 10);
  });

  return dirs;
}

function createNewSkill(plan, workspace) {
  const skillId = `auto_${plan.target}_${Math.random().toString(36).slice(2, 8)}_v1`;
  const skillPath = path.join(workspace, 'skills', skillId);
  fs.mkdirSync(skillPath, { recursive: true });

  const skillMd = generateSkillMd(skillId, plan.target, plan.improvements);
  const impl = generateImplementation({ category: plan.target });

  fs.writeFileSync(path.join(skillPath, 'SKILL.md'), skillMd, 'utf-8');
  fs.writeFileSync(path.join(skillPath, 'index.js'), impl, 'utf-8');

  return {
    success: true,
    changedFiles: [],
    newFiles: [path.join(skillPath, 'SKILL.md'), path.join(skillPath, 'index.js')],
  };
}

function generateSkillMd(skillId, target, improvements) {
  return `---
name: ${target}
description: "${improvements[0] || '自动生成的 Skill'}"
level: 1
auto_created: true
created_at: ${new Date().toISOString()}
---

# ${target}

## 功能
${improvements.map(i => `- ${i}`).join('\n')}

## 使用方式
\`\`\`javascript
const skill = require('./index.js');
skill.execute({ message: '测试消息' });
\`\`\`

## 验证
运行 \`node index.js --test\` 测试基本功能。
`;
}

function enhanceSkillMd(skillMdPath, improvements) {
  let content = fs.readFileSync(skillMdPath, 'utf-8');
  const updateSection = `\n## 自动改进记录\n${improvements.map(i => '- ' + new Date().toISOString().split('T')[0] + ': ' + i).join('\n')}\n`;

  if (!content.includes('## 自动改进记录')) {
    content += updateSection;
  } else {
    content = content.replace(
      /(## 自动改进记录\n)/,
      `$1${improvements.map(i => '- ' + new Date().toISOString().split('T')[0] + ': ' + i).join('\n')}\n`
    );
  }

  return content;
}

module.exports = {
  executeCodePlan,
  modifySkill,
  findSkillDirs,
  createNewSkill,
  generateSkillMd,
  enhanceSkillMd,
};
