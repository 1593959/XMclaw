#!/usr/bin/env node
'use strict';

const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');

const WORKSPACE = process.env.WORKSPACE || process.cwd();
const MEMORY_FILE = path.join(WORKSPACE, 'MEMORY.md');
const PROFILE_FILE = path.join(WORKSPACE, 'PROFILE.md');

function log(msg) {
  console.log('[' + new Date().toISOString() + '] ' + msg);
}

function getToday() {
  return new Date().toISOString().split('T')[0];
}

function extractInsights(logContent) {
  const insights = [];
  
  const fixMatches = logContent.match(/\|.*\|\s*✅\s*\|/g);
  if (fixMatches) {
    fixMatches.forEach(match => {
      const parts = match.split('|').filter(Boolean);
      if (parts[0] && !parts[0].includes('状态') && !parts[0].includes('问题')) {
        insights.push('修复: ' + parts[0].trim());
      }
    });
  }
  
  const evoMatches = logContent.match(/### \d{2}:\d{2} 进化循环/g);
  if (evoMatches) {
    insights.push('进化循环: ' + evoMatches.length + ' 次');
  }

  const geneMatches = logContent.match(/✅ 自动生成 Gene: (\S+)/g);
  if (geneMatches) {
    geneMatches.forEach(m => {
      const geneId = m.replace('✅ 自动生成 Gene: ', '');
      insights.push('Gene: ' + geneId);
    });
  }
  const skillMatches = logContent.match(/🛠️\s+自动创建 Skill: (\S+)/g);
  if (skillMatches) {
    skillMatches.forEach(m => {
      const skillId = m.replace(/🛠️\s+自动创建 Skill: /, '');
      insights.push('Skill: ' + skillId);
    });
  }
  
  const lessonMatches = logContent.match(/\*\*教训\*\*[：:]\s*(.+)/g);
  if (lessonMatches) {
    lessonMatches.forEach(m => {
      const lesson = m.replace(/\*\*教训\*\*[：:]\s*/, '');
      insights.push('教训: ' + lesson);
    });
  }
  
  const insightMatches = logContent.match(/\*\*关键领悟\*\*[：:]?\s*(.+)/g);
  if (insightMatches) {
    insightMatches.forEach(m => {
      const insight = m.replace(/\*\*关键领悟\*\*[：:]?\s*/, '');
      insights.push('领悟: ' + insight);
    });
  }

  const dailyInsightMatches = logContent.match(/## 今日洞察 \[.*?\]\n([\s\S]*?)(?=\n## |$)/g);
  const seenInsights = new Set();
  if (dailyInsightMatches) {
    dailyInsightMatches.forEach(section => {
      const titles = section.match(/- \*\*(.+?)\*\*/g);
      if (titles) {
        titles.forEach(t => {
          const title = t.replace(/- \*\*/, '').replace(/\*\*$/, '');
          if (!seenInsights.has(title)) {
            seenInsights.add(title);
            insights.push('洞察: ' + title);
          }
        });
      }
    });
  }

  const statusMatch = logContent.match(/Gene: (\d+).*Capsule: (\d+).*事件数: (\d+)/);
  if (statusMatch) {
    insights.push('状态: Gene:' + statusMatch[1] + ' Capsule:' + statusMatch[2] + ' 事件:' + statusMatch[3]);
  }

  const projectMatches = logContent.match(/### (.+?)\s*✅\s*\n- \*\*项目路径\*\*: `(.+?)`/g);
  if (projectMatches) {
    projectMatches.forEach(m => {
      const name = m.match(/### (.+?)\s*✅/)[1].trim();
      const projPath = m.match(/- \*\*项目路径\*\*: `(.+?)`/)[1].trim();
      insights.push('项目: ' + name + ' | 路径: ' + projPath);
    });
  }

  const completedMatches = logContent.match(/- ✅\s*(.+)/g);
  if (completedMatches) {
    completedMatches.forEach(m => {
      const item = m.replace(/- ✅\s*/, '').trim();
      if (item.length > 3 && !insights.some(i => i.includes(item))) {
        insights.push('完成: ' + item);
      }
    });
  }

  return [...new Set(insights)];
}

function autoCompleteTodos(logContent) {
  const completedPatterns = [
    { pattern: /实现 Gene 代码生成器[\s\S]*?自测通过|code_generator\.js[\s\S]*?成功|executeCodePlan/g, todoId: 'mnynrdyf', desc: '实现 Gene 代码生成器' },
    { pattern: /自动验证闭环[\s\S]*?通过|validation[\s\S]*?真实运行|solidify.*回滚修复/g, todoId: 'mnynrmru', desc: '建立自动验证闭环' },
    { pattern: /错误驱动修复[\s\S]*?通过|error_repair\.js[\s\S]*?成功|repairSkill/g, todoId: 'mnynrmt2', desc: '实现错误驱动修复' },
    { pattern: /Skill 实质进化[\s\S]*?index\.js|skill_maker.*默认生成|实质产出/g, todoId: 'mnynrmu8', desc: '重构 Skill 实质进化' },
    { pattern: /跨会话记忆[\s\S]*?MEMORY\.md|记忆加载[\s\S]*?成功|加载 MEMORY/g, todoId: 'mnynrmvh', desc: '实现跨会话记忆加载' },
    { pattern: /整体进化循环[\s\S]*?跑通|进化成功|Gene.*生成.*Skill.*创建/g, todoId: 'mnynrmwp', desc: '验证整体进化循环' }
  ];

  const completed = [];
  completedPatterns.forEach(({ pattern, todoId, desc }) => {
    if (pattern.test(logContent)) {
      try {
        execSync('node ' + path.join(WORKSPACE, 'skills/todo/index.js') + ' done ' + todoId, { stdio: 'ignore' });
        completed.push(desc);
        log('✅ 自动标记 todo 完成: ' + desc);
      } catch (e) {
        log('⚠️ 标记 todo 失败: ' + desc + ' (' + e.message + ')');
      }
    }
  });
  return completed;
}

function extractProjects(logContent) {
  const projects = [];
  const projectBlockRegex = /###\s+(.+?)\s*([✅🔴⏳])\s*\n([\s\S]*?)(?=\n###\s+|\n##\s+|\n###\s*$|$)/g;
  let match;
  while ((match = projectBlockRegex.exec(logContent)) !== null) {
    const name = match[1].trim();
    const status = match[2].trim();
    const body = match[3];
    
    const pathMatch = body.match(/- \*\*项目路径\*\*: `(.+?)`/);
    const techMatch = body.match(/- \*\*技术栈\*\*: (.+)/);
    const funcMatch = body.match(/- \*\*功能\*\*: (.+)/);
    
    if (pathMatch) {
      projects.push({
        name,
        status: status === '✅' ? 'active' : (status === '🔴' ? 'blocked' : 'pending'),
        path: pathMatch[1].trim(),
        tech: techMatch ? techMatch[1].trim() : '',
        func: funcMatch ? funcMatch[1].trim() : ''
      });
    }
  }
  return projects;
}

function syncProjectsToProfile(projects) {
  if (!fs.existsSync(PROFILE_FILE) || projects.length === 0) return;
  
  let profile = fs.readFileSync(PROFILE_FILE, 'utf-8');
  const projectSectionStart = profile.indexOf('### 共同维护项目');
  const projectSectionEnd = profile.indexOf('### ', projectSectionStart + 1);
  
  if (projectSectionStart === -1) {
    const insertPoint = profile.indexOf('## 工作协作');
    if (insertPoint === -1) return;
    
    let newSection = '\n### 共同维护项目\n| 项目 | 网址/路径 | 角色 |\n|------|------|------|\n';
    projects.forEach(p => {
      newSection += '| ' + p.name + ' | ' + p.path + ' | 共同维护 |\n';
    });
    
    profile = profile.slice(0, insertPoint) + newSection + '\n' + profile.slice(insertPoint);
    fs.writeFileSync(PROFILE_FILE, profile, 'utf-8');
    log('✅ 已更新 PROFILE.md 项目列表');
  } else {
    let section = profile.substring(projectSectionStart, projectSectionEnd !== -1 ? projectSectionEnd : undefined);
    projects.forEach(p => {
      if (!section.includes(p.name)) {
        const tableEnd = section.lastIndexOf('|');
        const insertPos = section.indexOf('\n', tableEnd) + 1;
        const newRow = '| ' + p.name + ' | ' + p.path + ' | 共同维护 |\n';
        section = section.slice(0, insertPos) + newRow + section.slice(insertPos);
      }
    });
    profile = profile.substring(0, projectSectionStart) + section + (projectSectionEnd !== -1 ? profile.substring(projectSectionEnd) : '');
    fs.writeFileSync(PROFILE_FILE, profile, 'utf-8');
    log('✅ 已更新 PROFILE.md 项目列表');
  }
}

function updateMemory(insights, today) {
  let memory = '';
  if (fs.existsSync(MEMORY_FILE)) {
    memory = fs.readFileSync(MEMORY_FILE, 'utf-8');
  }
  
  const header = '## ' + today + ' 每日提炼';
  if (memory.includes(header)) {
    log('⚠️ 今日提炼已存在，跳过');
    return;
  }
  
  const newSection = ['\n---\n', header, ''].concat(insights.map(i => '- ' + i)).join('\n');
  
  fs.writeFileSync(MEMORY_FILE, memory + newSection + '\n', 'utf-8');
  log('✅ 已写入 MEMORY.md: ' + insights.length + ' 条');
}

function main() {
  const today = getToday();
  const logPath = path.join(WORKSPACE, 'memory', today + '.md');
  
  log('📖 读取今日日志: ' + logPath);
  
  if (!fs.existsSync(logPath)) {
    log('今日日志不存在');
    return;
  }
  
  const content = fs.readFileSync(logPath, 'utf-8');
  
  autoCompleteTodos(content);
  
  // 读取 buffer.md 中的待归档发现
  const bufferPath = path.join(WORKSPACE, "memory", "buffer.md");
  let bufferInsights = [];
  if (fs.existsSync(bufferPath)) {
    const bufferContent = fs.readFileSync(bufferPath, "utf-8");
    const todayHeader = "## " + today + " 待归档发现";
    const bIdx = bufferContent.indexOf(todayHeader);
    if (bIdx >= 0) {
      let endIdx = bufferContent.indexOf("\n## ", bIdx + 1);
      if (endIdx < 0) endIdx = bufferContent.length;
      const section = bufferContent.substring(bIdx + todayHeader.length, endIdx);
      const lines = section.split("\n").filter(l => l.trim().startsWith("- ["));
      bufferInsights = lines.map(l => "buffer: " + l.trim().replace(/^-\s*\[/, "").replace(/\]$/, "").substring(0, 100));
    }
  }

  
  const projects = extractProjects(content);
  if (projects.length > 0) {
    syncProjectsToProfile(projects);
  }
  
  let insights = extractInsights(content);
  if (bufferInsights.length > 0) {
    insights = insights.concat(bufferInsights);
  }
  
  log('💡 发现 ' + insights.length + ' 条洞察');
  insights.forEach(i => log('   - ' + i));
  
  updateMemory(insights, today);
}

main();
