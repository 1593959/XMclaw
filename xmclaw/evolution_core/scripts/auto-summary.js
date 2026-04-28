#!/usr/bin/env node
'use strict';

/**
 * 自动对话总结脚本 - xm-auto-evo 集成
 * 
 * 在以下情况调用：
 * 1. 对话结束前
 * 2. 重要决策完成后
 * 3. 遇到问题解决后
 * 
 * 功能：
 * - 提取关键信息
 * - 更新 MEMORY.md
 * - 记录经验教训
 * - 更新人格状态
 */

const fs = require('fs');
const path = require('path');

const WORKSPACE = process.env.WORKSPACE || process.cwd();
const MEMORY_FILE = path.join(WORKSPACE, 'MEMORY.md');
const PROFILE_FILE = path.join(WORKSPACE, 'PROFILE.md');
const DATA_DIR = path.join(__dirname, '..', 'data');
const PERSONALITY_FILE = path.join(DATA_DIR, 'personality.json');

/**
 * 添加记忆条目
 */
function addMemory(content, type = 'general') {
  try {
    const timestamp = getToday();
    const entry = `\n\n## ${timestamp} [${type}]\n${content}`;
    
    if (fs.existsSync(MEMORY_FILE)) {
      const current = fs.readFileSync(MEMORY_FILE, 'utf-8');
      fs.writeFileSync(MEMORY_FILE, current + entry);
    }
    return true;
  } catch (e) {
    console.error('记忆添加失败:', e.message);
    return false;
  }
}

/**
 * 获取今天日期（北京时间）
 */
function getToday() {
  const now = new Date();
  const beijingTime = new Date(now.getTime() - now.getTimezoneOffset() * 60000);
  return beijingTime.toISOString().split('T')[0];
}

/**
 * 轻量级快速记录（关键节点用）
 */
function addQuickNote(content) {
  try {
    const timestamp = getToday();
    const time = new Date().toTimeString().split(' ')[0].substring(0, 5);
    
    // 查找或创建 ## 快速记录 section
    let memory = '';
    if (fs.existsSync(MEMORY_FILE)) {
      memory = fs.readFileSync(MEMORY_FILE, 'utf-8');
    }
    
    const sectionName = `## ${timestamp} 快速记录`;
    const noteEntry = `- [${time}] ${content}`;
    
    if (memory.includes(sectionName)) {
      // 追加到现有 section
      memory = memory.replace(sectionName, `${sectionName}\n${noteEntry}`);
    } else {
      // 添加新 section
      memory += `\n\n${sectionName}\n${noteEntry}`;
    }
    
    fs.writeFileSync(MEMORY_FILE, memory);
    return true;
  } catch (e) {
    console.error('快速记录失败:', e.message);
    return false;
  }
}

/**
 * 添加经验教训
 */
function addLesson(lesson, context = '') {
  const content = context 
    ? `**情境**: ${context}\n**教训**: ${lesson}`
    : lesson;
  return addMemory(content, 'lesson');
}

/**
 * 添加用户偏好
 */
function addPreference(key, value) {
  try {
    if (!fs.existsSync(PROFILE_FILE)) return false;
    
    let content = fs.readFileSync(PROFILE_FILE, 'utf-8');
    
    // 检查是否已存在
    const existing = new RegExp(`- \\*\\*${key}\\*\\*:.*`, 'i');
    if (existing.test(content)) {
      content = content.replace(existing, `- **${key}**: ${value}`);
    } else {
      // 找到 ### 偏好 section 末尾
      const prefSection = content.match(/### 偏好[\s\S]*?(?=###|$)/);
      if (prefSection) {
        content = content.replace(prefSection[0], prefSection[0] + `\n- **${key}**: ${value}`);
      }
    }
    
    fs.writeFileSync(PROFILE_FILE, content);
    return true;
  } catch (e) {
    console.error('偏好更新失败:', e.message);
    return false;
  }
}

/**
 * 更新人格状态
 */
function updatePersonality(updates) {
  try {
    if (!fs.existsSync(PERSONALITY_FILE)) return false;
    
    const personality = JSON.parse(fs.readFileSync(PERSONALITY_FILE, 'utf-8'));
    Object.assign(personality, updates, { last_updated: new Date().toISOString() });
    
    fs.writeFileSync(PERSONALITY_FILE, JSON.stringify(personality, null, 2));
    return true;
  } catch (e) {
    console.error('人格更新失败:', e.message);
    return false;
  }
}

/**
 * 记录成功
 */
function recordSuccess(action, result) {
  const content = `**行动**: ${action}\n**结果**: ${result}\n**时间**: ${new Date().toISOString()}`;
  addMemory(content, 'success');
  
  // 增加信心
  const personality = JSON.parse(fs.readFileSync(PERSONALITY_FILE, 'utf-8'));
  updatePersonality({ 
    confidence: Math.min(1, (personality.confidence || 0.5) + 0.05) 
  });
}

/**
 * 记录失败
 */
function recordFailure(action, error, solution = '') {
  const content = `**行动**: ${action}\n**错误**: ${error}${solution ? `\n**解决**: ${solution}` : ''}`;
  addMemory(content, 'failure');
  
  // 降低信心，增加风险规避
  const personality = JSON.parse(fs.readFileSync(PERSONALITY_FILE, 'utf-8'));
  updatePersonality({ 
    confidence: Math.max(0, (personality.confidence || 0.5) - 0.1),
    mood: 'cautious'
  });
}

/**
 * 从命令行参数提取总结
 */
function parseArgs() {
  const args = process.argv.slice(2);
  const result = { type: 'general', content: '' };
  
  for (let i = 0; i < args.length; i++) {
    switch (args[i]) {
      case '--quick':
        result.type = 'quick';
        result.content = args[++i] || '';
        break;
      case '--lesson':
        result.type = 'lesson';
        result.content = args[++i] || '';
        break;
      case '--success':
        result.type = 'success';
        result.content = args[++i] || '';
        break;
      case '--failure':
        result.type = 'failure';
        result.content = args[++i] || '';
        break;
      case '--preference':
        const [key, value] = (args[++i] || '').split('=');
        if (key && value) addPreference(key, value);
        return;
      case '--context':
        result.context = args[++i] || '';
        break;
      default:
        if (!args[i].startsWith('--')) {
          result.content += (result.content ? ' ' : '') + args[i];
        }
    }
  }
  
  return result;
}

// 主程序
function main() {
  const parsed = parseArgs();
  
  if (!parsed.content && parsed.type !== 'quick') {
    console.log('用法:');
    console.log('  # 轻量级快速记录（关键节点用）');
    console.log('  node auto-summary.js --quick "用户提到喜欢直接的方式"');
    console.log('');
    console.log('  # 经验教训');
    console.log('  node auto-summary.js --lesson "教训内容" --context "情境"');
    console.log('');
    console.log('  # 成功记录');
    console.log('  node auto-summary.js --success "成功内容"');
    console.log('');
    console.log('  # 失败记录');
    console.log('  node auto-summary.js --failure "失败内容" "解决方案"');
    console.log('');
    console.log('  # 用户偏好');
    console.log('  node auto-summary.js --preference "偏好=值"');
    return;
  }
  
  switch (parsed.type) {
    case 'quick':
      addQuickNote(parsed.content);
      console.log('✅ 快速记录已保存');
      break;
    case 'lesson':
      addLesson(parsed.content, parsed.context);
      console.log('✅ 经验教训已记录');
      break;
    case 'success':
      recordSuccess(parsed.content, '');
      console.log('✅ 成功已记录');
      break;
    case 'failure':
      recordFailure(parsed.content, '');
      console.log('✅ 失败已记录');
      break;
    default:
      addMemory(parsed.content);
      console.log('✅ 记忆已记录');
  }
}

main();
