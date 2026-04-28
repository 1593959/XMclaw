#!/usr/bin/env node
'use strict';

/**
 * 自动心跳脚本 - xm-auto-evo 集成
 * 
 * 每小时执行：
 * 1. 检查系统状态
 * 2. 运行进化循环
 * 3. 检查待办
 * 4. 记忆维护
 * 5. 自动写入 MEMORY.md
 */

const { execSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const WORKSPACE = process.env.WORKSPACE || process.cwd();
const MEMORY_FILE = path.join(WORKSPACE, 'MEMORY.md');
const DATA_DIR = path.join(__dirname, '..', 'data');

function log(msg) {
  console.log(`[${new Date().toISOString()}] ${msg}`);
}

function run(cmd) {
  try {
    return execSync(cmd, { cwd: WORKSPACE, encoding: 'utf-8', timeout: 120000 });
  } catch (e) {
    return e.stdout || e.message;
  }
}

async function main() {
  log('🚀 自动心跳开始');
  
  // 1. 检查系统状态
  log('📊 检查系统状态...');
  const status = run('node skills/xm-auto-evo/index.js status');
  console.log(status);
  
  // 2. 运行进化循环（如果距离上次超过 30 分钟）
  log('🔄 检查是否需要进化...');
  const lastEvo = getLastEvolutionTime();
  const now = Date.now();
  const minutesSinceLastEvo = lastEvo ? (now - lastEvo) / 60000 : 999;
  
  if (minutesSinceLastEvo >= 30) {
    log('⚙️ 运行进化循环...');
    const result = run('node skills/xm-auto-evo/index.js start');
    console.log(result);
    
    // 3. 自动更新 MEMORY.md
    log('📝 更新记忆...');
    await updateMemoryFromEvolution(result);
    
    // 4. 检查待办
    log('📋 检查待办事项...');
    const todoResult = run('node skills/todo/index.js list 2>/dev/null || echo "todo skill not found"');
    console.log(todoResult);
  } else {
    log(`⏭️ 距离上次进化 ${minutesSinceLastEvo.toFixed(0)} 分钟，跳过`);
  }
  
  log('✅ 自动心跳完成');
}

function getLastEvolutionTime() {
  try {
    const eventsFile = path.join(DATA_DIR, 'events.jsonl');
    if (!fs.existsSync(eventsFile)) return null;
    
    const lines = fs.readFileSync(eventsFile, 'utf-8').trim().split('\n').filter(Boolean);
    if (lines.length === 0) return null;
    
    const lastEvent = JSON.parse(lines[lines.length - 1]);
    return lastEvent.timestamp ? new Date(lastEvent.timestamp).getTime() : null;
  } catch {
    return null;
  }
}

async function updateMemoryFromEvolution(result) {
  try {
    // 提取进化结果
    const lines = result.split('\n');
    const insights = [];
    
    for (const line of lines) {
      if (line.includes('新 Gene') || line.includes('新 Skill') || line.includes('进化完成')) {
        insights.push(line.trim());
      }
    }
    
    if (insights.length > 0) {
      const timestamp = new Date().toISOString().split('T')[0];
      const entry = `\n\n## ${timestamp} 进化记录\n${insights.map(i => `- ${i}`).join('\n')}`;
      
      // 追加到 MEMORY.md
      if (fs.existsSync(MEMORY_FILE)) {
        const content = fs.readFileSync(MEMORY_FILE, 'utf-8');
        fs.writeFileSync(MEMORY_FILE, content + entry);
        log('📝 记忆已更新');
      }
    }
  } catch (e) {
    log(`⚠️ 记忆更新失败: ${e.message}`);
  }
}

// 运行
main().catch(console.error);
