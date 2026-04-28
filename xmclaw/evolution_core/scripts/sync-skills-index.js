/**
 * SKILLS_INDEX.md 同步脚本
 * 
 * 功能：
 * 1. 检查 skill.json 中所有技能
 * 2. 对比 SKILLS_INDEX.md
 * 3. 报告缺失的技能
 * 
 * 使用：
 * node skills/xm-auto-evo/scripts/sync-skills-index.js
 */

const fs = require('fs');
const path = require('path');

const WORKSPACE = process.cwd();
const SKILL_JSON = path.join(WORKSPACE, 'skill.json');
const SKILLS_INDEX = path.join(WORKSPACE, 'skills', 'SKILLS_INDEX.md');

function getInstalledSkills() {
  try {
    const skillJson = JSON.parse(fs.readFileSync(SKILL_JSON, 'utf-8'));
    const skills = skillJson.skills || {};
    return Object.keys(skills);
  } catch (e) {
    console.error('❌ 无法读取 skill.json:', e.message);
    return [];
  }
}

function getIndexedSkills() {
  try {
    const content = fs.readFileSync(SKILLS_INDEX, 'utf-8');
    const results = new Set();
    
    // 从技能地图表格中提取（格式: | **类别** | skill-name | description |）
    // 或者: | | skill-name | description |
    const lines = content.split('\n');
    for (const line of lines) {
      // 匹配表格行: | xxx | skill | desc |
      const match = line.match(/\|[^|]*\|\s*(\S+)\s*\|/);
      if (match && match[1] && !match[1].startsWith('**') && match[1] !== '类别' && match[1] !== '技能' && match[1] !== '说明') {
        results.add(match[1].toLowerCase());
      }
    }
    
    // 从详细索引中提取 ### N. xxx（skill-name）格式
    const indexMatches = content.matchAll(/### \d+\.\s+\S+\s*\((\S+)\)/g);
    for (const match of indexMatches) {
      results.add(match[1].toLowerCase());
    }
    
    return [...results];
  } catch (e) {
    console.error('读取 SKILLS_INDEX 失败:', e.message);
    return [];
  }
}

function main() {
  console.log('🔍 检查技能同步状态...\n');
  
  const installed = getInstalledSkills();
  const indexed = getIndexedSkills();
  
  console.log(`📦 skill.json 中的技能: ${installed.length}`);
  installed.forEach(s => console.log(`   - ${s}`));
  
  console.log(`\n📋 SKILLS_INDEX.md 中的技能: ${indexed.length}`);
  
  // 检查缺失
  const missing = installed.filter(s => !indexed.includes(s.toLowerCase()));
  
  if (missing.length > 0) {
    console.log('\n⚠️  缺失的技能（需要添加到 SKILLS_INDEX.md）：');
    missing.forEach(s => console.log(`   - ${s}`));
    console.log('\n💡 运行以下命令学习新技能：');
    missing.forEach(s => console.log(`   node skills/xm-auto-evo/index.js learn-skill ${s}`));
  } else {
    console.log('\n✅ 所有技能都已同步到 SKILLS_INDEX.md');
  }
}

main();
