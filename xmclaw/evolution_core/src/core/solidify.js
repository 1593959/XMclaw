'use strict';

/**
 * 固化协议 (Solidify Protocol)。
 *
 * 移植自 xm-evo/src/core/solidify.js
 *
 * 补丁应用后的验证闭环：
 * 1. 计算 blast radius
 * 2. 执行 Gene validation 命令
 * 3. ADL 约束检查
 * 4. 通过 → Capsule + Event | 失败 → 回滚
 */

const { execSync } = require('node:child_process');

/**
 * 计算变更的爆炸半径。
 */
function computeBlast(changedFiles, lineStats) {
  return {
    files: changedFiles ? changedFiles.length : 0,
    lines: lineStats ? (lineStats.additions || 0) + (lineStats.deletions || 0) : 0,
  };
}

/**
 * 执行验证命令列表。
 *
 * @param {string[]} commands - 验证命令
 * @param {string} [cwd] - 工作目录
 * @param {number} [timeoutMs=30000] - 超时（毫秒）
 * @returns {{ passed: boolean, results: Array }}
 */
function runValidations(commands, cwd, timeoutMs = 30000) {
  if (!commands || commands.length === 0) return { passed: true, results: [] };

  const results = [];
  let allPassed = true;

  for (const cmd of commands) {
    try {
      const output = execSync(cmd, {
        cwd: cwd || process.cwd(),
        timeout: timeoutMs,
        encoding: 'utf-8',
        stdio: ['pipe', 'pipe', 'pipe'],
      });
      results.push({ command: cmd, success: true, output: output.trim() });
    } catch (err) {
      allPassed = false;
      results.push({
        command: cmd,
        success: false,
        output: err.stderr ? err.stderr.toString().trim() : err.message,
      });
    }
  }

  return { passed: allPassed, results };
}

/**
 * 执行完整的固化流程。
 *
 * @param {Object} params - 固化参数
 * @param {Object} params.gene - 触发 Gene
 * @param {Object} params.mutation - 变异提案
 * @param {string[]} params.changedFiles - 变更文件
 * @param {string[]} [params.newFiles] - 新增文件
 * @param {Object} [params.lineStats] - 行统计
 * @param {boolean} [params.dryRun=false] - 干运行模式
 * @param {string} [params.cwd] - 工作目录
 * @param {boolean} [params.skipValidation=false] - 跳过验证（用于 skill 创建等软性能力）
 * @returns {Promise<Object>} 固化结果
 */
async function solidify(params) {
  // 延迟加载避免循环依赖
  const { checkADL } = require('../adl/lock');
  const { rollback } = require('../adl/rollback');
  const { addCapsule, appendEvent } = require('../gep/store');
  const { createEvent } = require('../gep/event');

  const {
    gene,
    mutation,
    changedFiles = [],
    newFiles = [],
    lineStats,
    dryRun = false,
    cwd,
    skipValidation = false,
  } = params;

  // 1. 计算 blast radius
  const blast = computeBlast([...changedFiles, ...newFiles], lineStats);

  // 2. 执行验证命令（如果 Gene 有 validation 字段）
  let validation = { passed: true, results: [] };
  if (!skipValidation && gene?.validation?.length > 0) {
    validation = runValidations(gene.validation, cwd);
  }

  // 3. ADL 约束检查
  let adl = null;
  if (checkADL) {
    adl = checkADL({ blast, mutation, gene });
  }

  // 判定是否通过
  const passed = validation.passed && (!adl || !adl.blocked);

  const result = {
    success: passed,
    blast,
    validation,
    adl,
    capsule: null,
    event: null,
    dryRun,
  };

  if (dryRun) {
    result.event = createEvent({
      event_type: passed ? 'solidify_dryrun_passed' : 'solidify_dryrun_failed',
      payload: { gene_id: gene?.id, mutation_category: mutation?.category, blast, validation, adl },
    });
    return result;
  }

  if (!passed) {
    // 失败 → 记录事件，回滚变更的文件
    if (rollback) {
      try {
        rollback(cwd || process.cwd(), changedFiles, newFiles);
      } catch (e) {
        console.error('Rollback failed:', e.message);
      }
    }
    result.event = createEvent({
      event_type: 'solidify_failed',
      payload: { gene_id: gene?.id, blast, validation, adl, reason: 'validation_or_adl_failed' },
    });
    appendEvent(result.event);
    return result;
  }

  // 4. 通过 → 记录 Capsule
  const capsule = {
    type: 'Capsule',
    id: `capsule_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`,
    gene_id: gene?.id || null,
    mutation_category: mutation?.category || 'repair',
    signals: mutation?.trigger_signals || [],
    files_changed: [...changedFiles, ...newFiles],
    summary: `${mutation?.category || 'repair'} 变异: ${mutation?.expected_effect || ''}`,
    metrics: {
      blast_files: blast.files,
      blast_lines: blast.lines,
      validation_passed: validation.passed,
    },
    created_at: new Date().toISOString(),
  };

  addCapsule(capsule);
  result.capsule = capsule;

  result.event = createEvent({
    event_type: 'solidify_success',
    payload: {
      gene_id: gene?.id,
      capsule_id: capsule.id,
      blast,
      mutation_category: mutation?.category,
    },
  });
  appendEvent(result.event);

  return result;
}

module.exports = { solidify, computeBlast, runValidations };
