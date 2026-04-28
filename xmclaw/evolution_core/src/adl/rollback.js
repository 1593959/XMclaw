'use strict';

/**
 * ADL 回滚机制 (xm-auto-evo 版)
 * 
 * 移植自 xm-evo/src/adl/rollback.js
 * 
 * 提供进化前快照创建和进化失败后的回滚能力。
 */

/** 保护路径，回滚时不删除 */
const PROTECTED_PATHS = ['package.json', 'SKILL.md', 'README.md', '.git/', 'node_modules/', 'assets/', 'config/'];

const { execSync } = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');

/**
 * 检查文件是否在保护路径下。
 */
function isProtected(filePath) {
  if (!filePath || typeof filePath !== 'string') return false;
  const normalized = filePath.replace(/\\/g, '/');
  for (const protectedPath of PROTECTED_PATHS) {
    if (protectedPath.endsWith('/')) {
      if (normalized.startsWith(protectedPath) || normalized.startsWith('./' + protectedPath)) {
        return true;
      }
    } else {
      if (normalized === protectedPath || normalized === './' + protectedPath) {
        return true;
      }
      if (normalized.endsWith('/' + protectedPath)) {
        return true;
      }
    }
  }
  return false;
}

/**
 * 在进化前创建快照（使用 git stash create）
 */
function createSnapshot(workDir) {
  try {
    const result = execSync('git stash create', {
      cwd: workDir,
      encoding: 'utf-8',
      stdio: ['pipe', 'pipe', 'pipe'],
    }).trim();
    if (!result) {
      return { success: true, snapshot: null, error: null };
    }
    return { success: true, snapshot: result, error: null };
  } catch (err) {
    return { success: false, snapshot: null, error: err.message };
  }
}

/**
 * 执行回滚，恢复变更的文件
 */
function rollback(workDir, changedFiles, newFiles) {
  const restored = [];
  const deleted = [];
  const errors = [];

  if (Array.isArray(changedFiles)) {
    for (const file of changedFiles) {
      try {
        execSync(`git checkout -- ${JSON.stringify(file)}`, {
          cwd: workDir,
          encoding: 'utf-8',
        });
        restored.push(file);
      } catch (err) {
        errors.push(`恢复失败 ${file}: ${err.message}`);
      }
    }
  }

  if (Array.isArray(newFiles)) {
    for (const file of newFiles) {
      if (isProtected(file)) {
        continue;
      }
      try {
        const fullPath = path.join(workDir, file);
        fs.unlinkSync(fullPath);
        deleted.push(file);
      } catch (err) {
        errors.push(`删除失败 ${file}: ${err.message}`);
      }
    }
  }

  return { success: errors.length === 0, restored, deleted, errors };
}

/**
 * 从快照恢复
 */
function restoreFromSnapshot(workDir, snapshot) {
  if (!snapshot) {
    return { success: false, error: 'No snapshot to restore' };
  }
  try {
    execSync(`git checkout ${snapshot}`, { cwd: workDir, encoding: 'utf-8' });
    return { success: true };
  } catch (err) {
    return { success: false, error: err.message };
  }
}

module.exports = { isProtected, createSnapshot, rollback, restoreFromSnapshot };
