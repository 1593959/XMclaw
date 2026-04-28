#!/usr/bin/env node
'use strict';

/**
 * 今日状态报告脚本
 * 
 * 生成今日工作摘要，用于回答"今天干了什么"
 */

const fs = require('fs');
const path = require('path');

const WORKSPACE = process.env.WORKSPACE || process.cwd();

function log(msg) {
  console.log(msg);
}

/**
 * 获取今天日期（北京时间）
 */
function getToday() {
  const now = new Date();
  // getTimezoneOffset() 返回负值（东八区是 -480），所以减法相当于加 8 小时
  const beijingTime = new Date(now.getTime() - now.getTimezoneOffset() * 60000);
  return beijingTime.toISOString().split('T')[0];
}

/**
 * 生成今日状态报告
 */
function generateReport() {
  const today = getToday();
  const logPath = path.join(WORKSPACE, 'memory', `${today}.md`);
  
  if (!fs.existsSync(logPath)) {
    log(`📅 今天是 ${today}，暂无工作日志`);
    return;
  }
  
  const content = fs.readFileSync(logPath, 'utf-8');
  
  log(`\n📅 今日状态报告 - ${today}`);
  log('═'.repeat(50));
  
  // 1. 核心事件
  const coreEvents = content.match(/## 今日核心事件\n([\s\S]*?)(?=##|$)/);
  if (coreEvents && coreEvents[1]) {
    log('\n🎯 今日核心事件：');
    
    // 提取 ### 标题
    const eventTitles = coreEvents[1].match(/### (.+)/g);
    if (eventTitles) {
      eventTitles.forEach(title => {
        log(`   • ${title.replace('### ', '')}`);
      });
    }
  }
  
  // 2. 进化记录统计
  const evoRecords = content.match(/### \d{2}:\d{2} 进化循环/g);
  if (evoRecords) {
    log(`\n🔄 进化循环: ${evoRecords.length} 次`);
  }
  
  // 3. 成功项
  const successItems = content.match(/\|.*✅.*\|/g);
  if (successItems) {
    log(`\n✅ 完成事项: ${successItems.length} 项`);
    successItems.slice(0, 5).forEach(item => {
      const parts = item.split('|').filter(Boolean);
      if (parts[0] && !parts[0].includes('状态') && !parts[0].includes('问题')) {
        log(`   • ${parts[0].trim()}`);
      }
    });
    if (successItems.length > 5) {
      log(`   ... 还有 ${successItems.length - 5} 项`);
    }
  }
  
  // 4. 快速记录
  const quickNotes = content.match(/- \[(\d{2}:\d{2})\] .+/g);
  if (quickNotes) {
    log(`\n📝 快速记录: ${quickNotes.length} 条`);
    quickNotes.slice(-3).forEach(note => {
      log(`   ${note.replace('- ', '')}`);
    });
  }
  
  // 5. 系统状态
  const statusMatch = content.match(/Gene: (\d+).*Capsule: (\d+).*事件数: (\d+)/);
  if (statusMatch) {
    log(`\n📊 系统状态: Gene:${statusMatch[1]} | Capsule:${statusMatch[2]} | 事件:${statusMatch[3]}`);
  }
  
  log('\n' + '═'.repeat(50));
  log(`📁 完整日志: memory/${today}.md`);
}

/**
 * 简版报告（用于快速回答）
 */
function quickReport() {
  const today = getToday();
  const logPath = path.join(WORKSPACE, 'memory', `${today}.md`);
  
  if (!fs.existsSync(logPath)) {
    return `今天是 ${today}，暂无工作日志`;
  }
  
  const content = fs.readFileSync(logPath, 'utf-8');
  
  // 提取前几条核心事件
  const events = content.match(/### (.+)/g);
  const evoCount = (content.match(/### \d{2}:\d{2} 进化循环/g) || []).length;
  
  let report = `📅 ${today} | `;
  
  if (events) {
    report += `核心事件 ${events.length} 项 | `;
  }
  
  report += `进化循环 ${evoCount} 次`;
  
  return report;
}

// 根据参数决定输出格式
const args = process.argv.slice(2);

if (args.includes('--quick') || args.includes('-q')) {
  log(quickReport());
} else {
  generateReport();
}
