#!/usr/bin/env node
'use strict';

/**
 * 修复 skill.json 中 auto_* skill 的 signature 字段
 * 
 * 问题：历史版本写入时 signature 为空
 * 解决方案：从 SKILL.md 中提取 triggers 并回填
 */

const fs = require('node:fs');
const path = require('path');

// 配置
const WORKSPACE = process.cwd();
const SKILL_JSON_PATH = path.join(WORKSPACE, 'skill.json');
const SKILLS_DIR = path.join(WORKSPACE, 'skills');

function extractSignatureFromSkillMd(skillDir) {
  const skillMdPath = path.join(skillDir, 'SKILL.md');
  if (!fs.existsSync(skillMdPath)) return null;
  
  const content = fs.readFileSync(skillMdPath, 'utf-8');
  const triggersMatch = content.match(/triggers:\s*\n([\s\S]*?)(?:```|$)/);
  
  if (triggersMatch) {
    const triggers = triggersMatch[1]
      .split('\n')
      .map(line => line.trim().replace(/^-\s*/, ''))
      .filter(Boolean);
    
    return triggers.join(',');
  }
  
  // 尝试从 description 中提取
  const descMatch = content.match(/description:\s*"?([^"\n]+)"?/);
  if (descMatch) {
    const desc = descMatch[1];
    // 检查是否包含跨会话模式
    if (desc.includes('跨会话问题反馈')) {
      const match = desc.match(/:\s*(\S+feedback)/);
      if (match) return `cross_session_${match[1]}`;
    }
  }
  
  return null;
}

function main() {
  console.log('\n🔧 修复 auto_* Skill 的 signature 字段\n');
  
  // 读取 skill.json
  let skillJson = { skills: {} };
  if (fs.existsSync(SKILL_JSON_PATH)) {
    try {
      skillJson = JSON.parse(fs.readFileSync(SKILL_JSON_PATH, 'utf-8'));
    } catch (e) {
      console.error('❌ 读取 skill.json 失败:', e.message);
      process.exit(1);
    }
  }
  
  const skills = skillJson.skills || {};
  let fixedCount = 0;
  let alreadyOkCount = 0;
  
  for (const [skillId, skill] of Object.entries(skills)) {
    if (!skillId.startsWith('auto_')) continue;
    
    const metadata = skill.metadata || {};
    
    // 检查是否需要修复
    const currentSignature = metadata.signature;
    const hasEmptySignature = !currentSignature || currentSignature === '';
    
    // 查找对应的 skill 目录
    const skillDir = path.join(SKILLS_DIR, skillId);
    
    if (!fs.existsSync(skillDir)) {
      console.log(`   ⚠️  目录不存在: ${skillId}`);
      continue;
    }
    
    const extractedSignature = extractSignatureFromSkillMd(skillDir);
    
    if (!extractedSignature) {
      console.log(`   ⚠️  无法提取 signature: ${skillId}`);
      continue;
    }
    
    if (hasEmptySignature) {
      // 需要修复
      metadata.signature = extractedSignature;
      skill.metadata = metadata;
      fixedCount++;
      console.log(`   ✅ 修复: ${skillId}`);
      console.log(`      旧值: "${currentSignature}"`);
      console.log(`      新值: "${extractedSignature}"`);
    } else if (currentSignature !== extractedSignature) {
      // signature 存在但不一致
      console.log(`   ⚠️  不一致: ${skillId}`);
      console.log(`      skill.json: "${currentSignature}"`);
      console.log(`      SKILL.md:   "${extractedSignature}"`);
      console.log(`      保持原值 (可手动确认)`);
    } else {
      alreadyOkCount++;
    }
  }
  
  // 保存修复后的 skill.json
  if (fixedCount > 0) {
    fs.writeFileSync(SKILL_JSON_PATH, JSON.stringify(skillJson, null, 2), 'utf-8');
    console.log(`\n✅ 已修复 ${fixedCount} 个 skill`);
  } else {
    console.log(`\n📝 无需修复 (已修复: ${alreadyOkCount} 个)`);
  }
  
  // 输出统计
  const autoSkillCount = Object.keys(skills).filter(k => k.startsWith('auto_')).length;
  console.log(`\n📊 统计: auto_* Skill 总数: ${autoSkillCount}`);
}

main();
