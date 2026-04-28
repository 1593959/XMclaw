#!/usr/bin/env node
'use strict';

const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');

const WORKSPACE = process.env.WORKSPACE || process.cwd();
const BUFFER_FILE = path.join(WORKSPACE, 'memory', 'buffer.md');
const MEMORY_FILE = path.join(WORKSPACE, 'MEMORY.md');
const PROFILE_FILE = path.join(WORKSPACE, 'PROFILE.md');

function log(msg) {
  console.log('[' + new Date().toISOString() + '] ' + msg);
}

function getToday() {
  return new Date().toISOString().split('T')[0];
}

function getRecentDialogFiles(hours = 1) {
  const dialogDir = path.join(WORKSPACE, 'dialog');
  if (!fs.existsSync(dialogDir)) return [];
  
  const now = Date.now();
  const cutoff = now - hours * 60 * 60 * 1000;
  
  const files = fs.readdirSync(dialogDir)
    .filter(f => f.endsWith('.jsonl'))
    .map(f => {
      const fp = path.join(dialogDir, f);
      const stat = fs.statSync(fp);
      return { file: f, path: fp, mtime: stat.mtimeMs };
    })
    .filter(x => x.mtime >= cutoff)
    .sort((a, b) => b.mtime - a.mtime);
  
  return files;
}

function readDialogContent(files, maxLines = 200) {
  const lines = [];
  for (const f of files) {
    try {
      const content = fs.readFileSync(f.path, 'utf-8');
      const fileLines = content.split('\n').filter(Boolean);
      lines.push(...fileLines.slice(-Math.floor(maxLines / files.length)));
    } catch (e) {
      log('⚠️ 读取失败: ' + f.file);
    }
  }
  return lines.map(l => {
    try {
      return JSON.parse(l);
    } catch {
      return null;
    }
  }).filter(Boolean);
}

function extractTextFromMessages(messages) {
  const texts = [];
  for (const msg of messages) {
    if (!msg.content || !Array.isArray(msg.content)) continue;
    for (const part of msg.content) {
      if (part.type === 'text' && part.text) {
        texts.push(part.text);
      }
    }
  }
  return texts.join('\n');
}

function detectPotentialOmissions(text) {
  const findings = [];
  
  const checks = [
    {
      type: 'project',
      target: 'MEMORY.md',
      patterns: [
        /项目[路径名称]*[:：]\s*`?([^`\n]+)`?/i,
        /技术栈[:：]\s*(.+)/i,
        /功能[:：]\s*(.+)/i,
        /已完成[:：]\s*✅\s*(.+)/i,
        /待解决[:：]\s*🔴\s*(.+)/i
      ]
    },
    {
      type: 'lesson',
      target: 'MEMORY.md',
      patterns: [
        /教训[:：]\s*(.+)/i,
        /规则[:：]\s*(.+)/i,
        /注意[:：]\s*(.+)/i,
        /不能(.+)[，。]/i,
        /必须(.+)[，。]/i,
        /应优先(.+)[，。]/i
      ]
    },
    {
      type: 'preference',
      target: 'PROFILE.md',
      patterns: [
        /希望(.+)[，。]/i,
        /喜欢(.+)[，。]/i,
        /不要(.+)[，。]/i,
        /偏好(.+)[，。]/i,
        /期望(.+)[，。]/i
      ]
    },
    {
      type: 'config',
      target: 'MEMORY.md',
      patterns: [
        /代理[:：]\s*(.+)/i,
        /端口[:：]\s*(\d+)/i,
        /API\s*Key[:：]\s*(.+)/i,
        /路径[:：]\s*`?([^`\n]+)`?/i,
        /配置了(.+)[，。]/i
      ]
    }
  ];
  
  for (const check of checks) {
    for (const pattern of check.patterns) {
      const matches = text.match(new RegExp(pattern, 'gi'));
      if (matches) {
        matches.forEach(m => {
          const clean = m.replace(/\n/g, ' ').trim().substring(0, 120);
          findings.push({
            type: check.type,
            target: check.target,
            content: clean,
            timestamp: new Date().toISOString()
          });
        });
      }
    }
  }
  
  return findings;
}

function dedupFindings(findings) {
  const seen = new Set();
  return findings.filter(f => {
    const key = f.type + ':' + f.content;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function filterAlreadySynced(findings) {
  let memory = '';
  let profile = '';
  
  try {
    memory = fs.readFileSync(MEMORY_FILE, 'utf-8');
  } catch {}
  try {
    profile = fs.readFileSync(PROFILE_FILE, 'utf-8');
  } catch {}
  
  return findings.filter(f => {
    const searchText = f.content.substring(0, 60);
    if (f.target === 'MEMORY.md' && memory.includes(searchText)) return false;
    if (f.target === 'PROFILE.md' && profile.includes(searchText)) return false;
    return true;
  });
}

function appendToBuffer(findings) {
  if (findings.length === 0) return;
  
  let buffer = '';
  if (fs.existsSync(BUFFER_FILE)) {
    buffer = fs.readFileSync(BUFFER_FILE, 'utf-8');
  }
  
  const today = getToday();
  const header = '## ' + today + ' 待归档发现';
  
  let section = '';
  if (!buffer.includes(header)) {
    section += '\n' + header + '\n\n';
  }
  
  for (const f of findings) {
    section += '- [' + f.type + ' → ' + f.target + '] ' + f.content + '\n';
  }
  
  fs.writeFileSync(BUFFER_FILE, buffer + section + '\n', 'utf-8');
  log('✅ 已写入 buffer: ' + findings.length + ' 条');
}

function main() {
  log('🚀 启动 post-dialog-check');
  
  const files = getRecentDialogFiles(1);
  log('📁 最近1小时对话文件: ' + files.length + ' 个');
  
  if (files.length === 0) {
    log('无新对话，跳过');
    return;
  }
  
  const messages = readDialogContent(files);
  const text = extractTextFromMessages(messages);
  
  log('📝 读取消息长度: ' + text.length + ' 字符');
  
  if (text.length < 50) {
    log('内容过短，跳过');
    return;
  }
  
  let findings = detectPotentialOmissions(text);
  findings = dedupFindings(findings);
  findings = filterAlreadySynced(findings);
  
  log('🔍 发现潜在遗漏: ' + findings.length + ' 条');
  findings.forEach(f => log('   - [' + f.type + '] ' + f.content.substring(0, 80)));
  
  appendToBuffer(findings);
  
  log('✅ post-dialog-check 完成');
}

main();
