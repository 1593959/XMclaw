'use strict';

/**
 * 错误驱动修复器 (Error-Driven Repair)
 *
 * 根据 validation 失败的 stderr 输出，自动分析错误并修复代码。
 */

const fs = require('node:fs');
const path = require('node:path');

/**
 * 解析 stderr，提取关键错误信息
 */
function parseErrors(stderr) {
  if (!stderr) return [];
  const errors = [];
  const lines = stderr.split('\n');

  for (const line of lines) {
    // SyntaxError
    const syntaxMatch = line.match(/SyntaxError:\s*(.+?)(?:\s+at\s+|\s*\(?(.+?):(\d+):(\d+)\)?)/i)
      || line.match(/SyntaxError:\s*(.+)/i);
    if (syntaxMatch) {
      errors.push({ type: 'syntax', message: syntaxMatch[1].trim(), file: syntaxMatch[2] ? syntaxMatch[2].trim() : undefined, line: syntaxMatch[3] ? parseInt(syntaxMatch[3], 10) : undefined });
      continue;
    }
    // ReferenceError: with or without 'is not defined'
    const refMatch = line.match(/ReferenceError:\s*([^(]+?)(?:\s+is not defined)(?:\s*\(?([^:]+?):(\d+):(\d+)\)?)?/i)
      || line.match(/ReferenceError:\s*([^(]+?)(?:\s*\(?([^:]+?):(\d+):(\d+)\)?)?/i);
    if (refMatch) {
      let msg = refMatch[1].trim();
      if (line.includes('is not defined') && !msg.includes('is not defined')) {
        msg += ' is not defined';
      }
      errors.push({ type: 'reference', message: msg, file: refMatch[2] ? refMatch[2].trim() : undefined, line: refMatch[3] ? parseInt(refMatch[3], 10) : undefined });
      continue;
    }
    // Module not found
    const moduleMatch = line.match(/Error:\s*Cannot find module ['"](.+?)['"]/i);
    if (moduleMatch) {
      errors.push({ type: 'module_not_found', message: `Cannot find module: ${moduleMatch[1]}`, module: moduleMatch[1] });
      continue;
    }
    // TypeError
    const typeMatch = line.match(/TypeError:\s*(.+?)(?:\s*\(?(.+?):(\d+):(\d+)\)?)?/i);
    if (typeMatch) {
      errors.push({ type: 'type', message: typeMatch[1].trim(), file: typeMatch[2] ? typeMatch[2].trim() : undefined, line: typeMatch[3] ? parseInt(typeMatch[3], 10) : undefined });
      continue;
    }
  }

  return errors;
}

/**
 * 尝试修复代码
 */
function repairCode(filePath, content, errors) {
  if (!errors || errors.length === 0) {
    return { success: false, content, fixes: [] };
  }

  let repaired = content;
  const fixes = [];

  for (const err of errors) {
    if (err.type === 'syntax') {
      const r = fixSyntaxError(repaired, err);
      if (r.fixed) { repaired = r.content; fixes.push(`语法修复: ${err.message}`); }
    } else if (err.type === 'reference') {
      const r = fixReferenceError(repaired, err);
      if (r.fixed) { repaired = r.content; fixes.push(`引用修复: ${err.message}`); }
    } else if (err.type === 'module_not_found') {
      const r = fixModuleNotFound(repaired, err);
      if (r.fixed) { repaired = r.content; fixes.push(`模块修复: ${err.message}`); }
    } else if (err.type === 'type') {
      const r = fixTypeError(repaired, err);
      if (r.fixed) { repaired = r.content; fixes.push(`类型修复: ${err.message}`); }
    }
  }

  if (fixes.length === 0) {
    const r = applyConservativeFallback(repaired);
    if (r.fixed) { repaired = r.content; fixes.push('保守回滚: 移除最近自动添加的注释/代码块'); }
  }

  return { success: fixes.length > 0, content: repaired, fixes };
}

function fixSyntaxError(content, err) {
  let fixed = content;

  if (err.message.includes('Invalid or unexpected token') || err.message.includes('Invalid escape') || err.message.includes('Invalid Unicode escape')) {
    // 修复字符串字面量中的非法转义
    fixed = fixEscapesInStrings(fixed);
    return { fixed: fixed !== content, content: fixed };
  }

  return { fixed: false, content };
}

/**
 * 修复字符串字面量中的非法转义序列
 * \s \w \d 等正则转义 -> \\s \\w \\d
 * \u 后面不是4位hex -> \\u
 * \x 后面不是2位hex -> \\x
 * 其他常见非法转义同理
 */
function fixEscapesInStrings(content) {
  let result = '';
  let i = 0;
  while (i < content.length) {
    const ch = content[i];
    if (ch === '"' || ch === "'") {
      let j = i + 1;
      while (j < content.length) {
        if (content[j] === '\\') {
          j += 2;
        } else if (content[j] === ch) {
          j++;
          break;
        } else {
          j++;
        }
      }
      let str = content.slice(i, j);
      str = fixStringEscapes(str);
      result += str;
      i = j;
    } else {
      result += ch;
      i++;
    }
  }
  return result;
}

function fixStringEscapes(str) {
  let fixed = str;
  const regexChars = ['s', 'w', 'd', 'b', 'S', 'W', 'D', 'B'];
  for (const char of regexChars) {
    fixed = fixed.replace(new RegExp('\\\\(\\\\' + char + ')', 'g'), '$1'); // 避免双重修复
    fixed = fixed.replace(new RegExp('(?<!\\\\)\\\\' + char, 'g'), '\\\\$&');
  }
  // \u 后面不是4位hex数字
  fixed = fixed.replace(/\\u(?![0-9a-fA-F]{4})/g, '\\\\u');
  // \x 后面不是2位hex数字
  fixed = fixed.replace(/\\x(?![0-9a-fA-F]{2})/g, '\\\\x');
  // 单独的反斜杠（行尾或后面跟普通字符）
  // 这个比较复杂，先不处理
  return fixed;
}

function fixReferenceError(content, err) {
  const missingVarMatch = err.message.match(/(.+?)\s+is not defined/i);
  if (!missingVarMatch) return { fixed: false, content };

  const varName = missingVarMatch[1].trim();
  const commonRequires = {
    fs: "const fs = require('node:fs');",
    path: "const path = require('node:path');",
    crypto: "const crypto = require('node:crypto');",
    os: "const os = require('node:os');",
    url: "const url = require('node:url');",
    http: "const http = require('node:http');",
    https: "const https = require('node:https');",
    child_process: "const { execSync } = require('node:child_process');",
    util: "const util = require('node:util');",
  };

  if (commonRequires[varName]) {
    if (content.includes(commonRequires[varName]) || content.includes(`require('${varName}')`) || content.includes(`require('node:${varName}')`)) {
      return { fixed: false, content };
    }
    const lines = content.split('\n');
    let insertIndex = 0;
    while (insertIndex < lines.length && (lines[insertIndex].trim() === '' || lines[insertIndex].startsWith('/*') || lines[insertIndex].startsWith(' *') || lines[insertIndex].startsWith('//') || lines[insertIndex].includes('use strict'))) {
      insertIndex++;
    }
    lines.splice(insertIndex, 0, commonRequires[varName]);
    return { fixed: true, content: lines.join('\n') };
  }

  return { fixed: false, content };
}

function fixModuleNotFound(content, err) {
  // 如果缺失的是相对路径模块，尝试修正路径
  const mod = err.module || '';
  if (mod.startsWith('./') || mod.startsWith('../')) {
    // 简单策略：如果 require 的是一个目录，尝试补 index.js
    const escapedMod = mod.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const requireMatch = content.match(new RegExp("require\\(['\"]" + escapedMod + "['\"]\\)"));
    if (requireMatch) {
      const fixed = content.replace(requireMatch[0], "require('" + mod + "/index.js')");
      return { fixed: true, content: fixed };
    }
  }
  return { fixed: false, content };
}

function fixTypeError(content, err) {
  // 常见 TypeError: x is not a function -> 尝试添加空函数兜底
  const notFuncMatch = err.message.match(/(.+?)\s+is not a function/i);
  if (notFuncMatch) {
    const funcName = notFuncMatch[1].trim();
    if (!content.includes(`function ${funcName}`) && !content.includes(`const ${funcName} =`) && !content.includes(`let ${funcName} =`) && !content.includes(`var ${funcName} =`)) {
      const stub = `\n// Auto-generated stub for ${funcName}\nfunction ${funcName}(...args) {\n  console.warn('Stub called:', '${funcName}', args);\n  return args[0];\n}\n`;
      return { fixed: true, content: content + stub };
    }
  }
  return { fixed: false, content };
}

function applyConservativeFallback(content) {
  // 移除 AUTO-EVO 自动添加的注释块（通常是导致语法问题的根源）
  const autoCommentPattern = /\/\*\s*\[AUTO-EVO\][\s\S]*?\*\//g;
  const cleaned = content.replace(autoCommentPattern, '');
  if (cleaned !== content) {
    return { fixed: true, content: cleaned.trim() };
  }
  return { fixed: false, content };
}

/**
 * 尝试修复 Skill 文件
 * @param {string} skillPath - Skill 目录路径
 * @param {string} stderr - validation 的错误输出
 * @returns {{success: boolean, fixes: string[]}}
 */
function repairSkill(skillPath, stderr) {
  const indexPath = path.join(skillPath, 'index.js');
  if (!fs.existsSync(indexPath)) {
    return { success: false, fixes: ['index.js 不存在'] };
  }

  const content = fs.readFileSync(indexPath, 'utf-8');
  const errors = parseErrors(stderr);
  if (errors.length === 0) {
    return { success: false, fixes: ['无法解析错误信息'] };
  }

  const result = repairCode(indexPath, content, errors);
  if (result.success) {
    fs.writeFileSync(indexPath, result.content, 'utf-8');
  }
  return result;
}

module.exports = { parseErrors, repairCode, repairSkill };
