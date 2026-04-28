'use strict';

/**
 * PCEC 思维爆炸。
 *
 * 移植自 xm-evo/src/pcec/explosion.js
 *
 * 当系统停滞或需要突破时，生成发散性思考 prompt，
 * 并从 AI 生成的爆炸结果中提取可执行项。
 *
 * 注意：本模块只生成 prompt + 解析结果，
 * 实际的 LLM 调用由 engine 层通过 agent tool 执行。
 */

/** 基础思维爆炸问题 */
const EXPLOSION_QUESTIONS = [
  '如果彻底推翻当前默认做法，会发生什么？',
  '如果是系统设计者而非执行者，会删掉什么？',
  '如果让能力弱 10 倍的 agent 也能成功，需要补什么？',
  '如果这个能力被调用 1000 次，现在的设计是否必然崩溃？',
  '当前最大的隐性假设是什么？如果它是错的呢？',
  '哪些步骤可以完全自动化，使其永远不需要人类介入？',
  '如果从零重建这个能力，最小可行版本是什么？',
  '当前方案中，哪些部分只是习惯而非必要？',
];

/** 停滞时追加的激进问题 */
const STAGNANT_QUESTIONS = [
  '最近两个周期没有实质产出，系统是否陷入了"看起来在工作"的假象？',
  '如果强制要求这个周期必须改变一件事，最值得改变的是什么？',
  '是否存在某个能力已经过时，应该直接废弃而非修补？',
];

/** 失败频发时追加的修复导向问题 */
const FAILURE_QUESTIONS = [
  '最近频繁失败的根因是什么？是否在反复踩同一个坑？',
  '哪些失败可以通过增加前置检查完全避免？',
  '是否需要回退到一个已知稳定的状态，重新出发？',
];

function _pickRandom(arr, n) {
  const copy = arr.slice();
  const result = [];
  const count = Math.min(n, copy.length);
  for (let i = 0; i < count; i++) {
    const idx = Math.floor(Math.random() * copy.length);
    result.push(copy.splice(idx, 1)[0]);
  }
  return result;
}

/**
 * 生成思维爆炸 prompt。
 *
 * @param {Object} context - 上下文信息
 * @param {string[]} [context.currentCapabilities] - 当前已有能力列表
 * @param {string[]} [context.recentFailures] - 近期失败记录
 * @param {number} [context.stagnantCycles] - 连续停滞周期数
 * @param {string[]} [context.recentSignals] - 近期信号
 * @returns {{ questions: string[], focusArea: string, prompt: string }}
 */
function generateExplosion(context = {}) {
  const {
    currentCapabilities = [],
    recentFailures = [],
    stagnantCycles = 0,
    recentSignals = [],
  } = context;

  let pool = EXPLOSION_QUESTIONS.slice();
  if (stagnantCycles > 0) pool = pool.concat(STAGNANT_QUESTIONS);
  if (recentFailures.length > 2) pool = pool.concat(FAILURE_QUESTIONS);

  const pickCount = stagnantCycles > 1 ? 4 : 3;
  const questions = _pickRandom(pool, pickCount);

  // 确定聚焦领域
  let focusArea = '通用能力增强';
  if (recentSignals.length > 0) {
    const topSignal = recentSignals[0];
    if (topSignal.startsWith('tool_category:')) focusArea = `工具能力: ${topSignal.replace('tool_category:', '')}`;
    else if (topSignal.startsWith('intent:')) focusArea = `用户意图: ${topSignal.replace('intent:', '')}`;
    else if (topSignal.startsWith('tool_use:')) focusArea = `具体工具: ${topSignal.replace('tool_use:', '')}`;
  }

  const prompt = `【PCEC 思维爆炸】聚焦领域: ${focusArea}

请对以下 ${questions.length} 个问题进行深度思考，每个问题给出 2-3 句话的简短分析：

${questions.map((q, i) => `${i + 1}. ${q}`).join('\n')}

${currentCapabilities.length > 0 ? `当前已有能力：\n${currentCapabilities.map(c => `- ${c}`).join('\n')}\n` : ''}
${recentFailures.length > 0 ? `近期失败记录：\n${recentFailures.map(f => `- ${f}`).join('\n')}\n` : ''}

请按以下 JSON 格式输出（只需 JSON，不要其他内容）：
{
  "insights": [
    { "question": "问题原文", "analysis": "2-3句话分析", "actionable": "可执行的行动项或结论" }
  ],
  "priority_action": "最值得立即执行的行动",
  "should_abandon": ["如果某个习惯/做法应该被废弃，列在这里"],
  "radical_idea": "一个激进但可能带来突破的想法"
}`;

  return { questions, focusArea, prompt };
}

/**
 * 从思维爆炸响应中提取结构化信息。
 *
 * @param {string} response - LLM 返回的文本（应包含 JSON）
 * @returns {{ insights: Array, priority_action: string, should_abandon: string[], radical_idea: string }}
 */
function extractFromExplosion(response) {
  if (!response || typeof response !== 'string') {
    return { insights: [], priority_action: '', should_abandon: [], radical_idea: '' };
  }

  // 尝试提取 JSON 块
  const jsonMatch = response.match(/\{[\s\S]*\}/);
  if (!jsonMatch) {
    return { insights: [], priority_action: '', should_abandon: [], radical_idea: '' };
  }

  try {
    const parsed = JSON.parse(jsonMatch[0]);
    return {
      insights: Array.isArray(parsed.insights) ? parsed.insights : [],
      priority_action: parsed.priority_action || '',
      should_abandon: Array.isArray(parsed.should_abandon) ? parsed.should_abandon : [],
      radical_idea: parsed.radical_idea || '',
    };
  } catch {
    return { insights: [], priority_action: '', should_abandon: [], radical_idea: '' };
  }
}

module.exports = { generateExplosion, extractFromExplosion };
