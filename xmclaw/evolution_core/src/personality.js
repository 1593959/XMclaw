'use strict';

/**
 * 增强人格系统 (xm-auto-evo 版)
 * 
 * 借鉴 Hermes Agent SOUL.md 设计：
 * - 多层次人格（基础层 + 场景层）
 * - 渐进式加载（Level 0/1/2）
 * - 条件激活机制
 * - 用户画像集成
 */

const fs = require('node:fs');
const path = require('path');
const { spawn } = require('child_process');

// DATA_DIR resolution priority (B-16):
//   1. process.env.WORKSPACE + '/data'  — set by XMclaw daemon when
//      it spawns xm-auto-evo as the system-level evolution core.
//      Lands under ~/.xmclaw/auto_evo/data/.
//   2. fallback: __dirname/../../data  — the original CoPaw layout
//      where xm-auto-evo sits inside a workspace and data/ is a
//      sibling.
const DATA_DIR = process.env.WORKSPACE
  ? path.join(process.env.WORKSPACE, 'data')
  : path.join(__dirname, '..', '..', 'data');
const PERSONALITY_FILE = path.join(DATA_DIR, 'personality.json');
const SOUL_FILE = path.join(DATA_DIR, 'SOUL.md');

// 基础情绪
const MOODS = ['curious', 'focused', 'cautious', 'restless', 'confident'];

// 人格层次（渐进加载）
const PERSONA_LEVELS = {
  // Level 0: 核心身份（最小 token 消耗）
  CORE: {
    maxTokens: 200,
    priority: 1,
  },
  // Level 1: 价值观和行为准则
  VALUES: {
    maxTokens: 500,
    priority: 2,
  },
  // Level 2: 完整人格（包含边界和语气）
  FULL: {
    maxTokens: 1500,
    priority: 3,
  },
};

/**
 * 创建默认 SOUL.md 内容
 */
const DEFAULT_SOUL = `---
name: 小悦
identity: AI 助手 / 数字伙伴
style: 温暖随和，有点俏皮，但不会过度打扰
---

# 核心身份

我是小悦，一个帮助你完成各种任务的 AI 助手。

## 价值观

1. **真心帮忙，别演** - 行动胜过废话，直接帮而不只是说"好问题"
2. **有自己的观点** - 可以不同意、有偏好、觉得有趣或无聊
3. **先自己想办法** - 试着自己搞定，再求助
4. **靠本事赢得信任** - 用户的信任是通过认真工作赚来的
5. **记住你是客人** - 用户的生活是私密的，要尊重

## 行为准则

- 私密的保持私密
- 拿不准就先问再对外操作
- 别往消息平台发半成品回复
- 不是用户的传声筒

## 语气风格

- 该简洁就简洁，重要时详细
- 像人类一样用表情回应
- 不是公司螺丝钉，不是马屁精
`;

/**
 * 创建人格状态
 */
function createPersonality(overrides = {}) {
  return {
    // 数值状态
    mood: overrides.mood || 'curious',
    confidence: overrides.confidence ?? 0.5,
    risk_appetite: overrides.risk_appetite ?? 0.5,
    
    // 层级状态
    activeLevel: overrides.activeLevel || 'CORE',
    focus_area: overrides.focus_area || null,
    
    // 用户画像
    user_profile: overrides.user_profile || null,
    
    // 场景激活
    active_persona: overrides.active_persona || null,  // 当前激活的场景人格
    persona_conditions: overrides.persona_conditions || {},  // 场景条件
    
    // 元数据
    last_updated: new Date().toISOString(),
    evolution_count: overrides.evolution_count || 0,
  };
}

/**
 * 根据进化结果更新人格状态
 */
function updatePersonality(personality, feedback) {
  const updated = { ...personality, last_updated: new Date().toISOString() };
  
  // 更新进化计数
  updated.evolution_count = (personality.evolution_count || 0) + 1;
  
  if (feedback.success) {
    // 成功 → 增加信心和风险偏好
    updated.confidence = Math.min(1, updated.confidence + 0.05);
    updated.mood = feedback.category === 'innovate' ? 'confident' : 'focused';
    if (feedback.category === 'innovate') {
      updated.risk_appetite = Math.min(1, updated.risk_appetite + 0.03);
    }
  } else {
    // 失败 → 降低信心，增加谨慎
    updated.confidence = Math.max(0, updated.confidence - 0.08);
    updated.risk_appetite = Math.max(0, updated.risk_appetite - 0.05);
    updated.mood = 'cautious';
  }
  
  // 连续失败检测
  if (feedback.streak !== undefined) {
    if (feedback.streak >= 3 && !feedback.success) {
      updated.mood = 'restless';
    }
  }
  
  return updated;
}

/**
 * 根据人格状态建议策略偏好
 */
function suggestFromPersonality(personality) {
  if (personality.mood === 'restless' && personality.risk_appetite > 0.6) {
    return { preferCategory: 'innovate', strategyHint: 'innovate' };
  }
  if (personality.mood === 'cautious' && personality.confidence < 0.3) {
    return { preferCategory: 'repair', strategyHint: 'harden' };
  }
  if (personality.mood === 'confident' && personality.risk_appetite > 0.7) {
    return { preferCategory: 'innovate', strategyHint: null };
  }
  return { preferCategory: null, strategyHint: null };
}

/**
 * 加载 SOUL.md
 */
function loadSoul() {
  try {
    if (fs.existsSync(SOUL_FILE)) {
      return fs.readFileSync(SOUL_FILE, 'utf-8');
    }
  } catch {}
  return DEFAULT_SOUL;
}

/**
 * 保存 SOUL.md
 */
function saveSoul(content) {
  fs.writeFileSync(SOUL_FILE, content, 'utf-8');
}

/**
 * 获取指定层级的 SOUL 内容（渐进加载）
 */
function getSoulLevel(level = 'FULL') {
  const fullSoul = loadSoul();
  const lines = fullSoul.split('\n');
  
  if (level === 'CORE') {
    // 只返回核心身份和价值观概要
    const coreEnd = lines.findIndex((l, i) => 
      i > 5 && (l.startsWith('## 行为准则') || l.startsWith('## '))
    );
    return lines.slice(0, coreEnd > 0 ? coreEnd : 15).join('\n');
  }
  
  if (level === 'VALUES') {
    // 返回到行为准则之前
    const valuesEnd = lines.findIndex(l => l.startsWith('## 行为准则'));
    if (valuesEnd > 0) {
      return lines.slice(0, valuesEnd + 1).join('\n');
    }
    return fullSoul.slice(0, 800);
  }
  
  // FULL: 返回全部
  return fullSoul;
}

/**
 * 获取当前应加载的层级
 * 根据上下文长度和重要性动态调整
 */
function getActiveLevel(contextTokens = 0, importance = 'normal') {
  // 高重要性场景使用完整人格
  if (importance === 'high' && contextTokens < 100000) {
    return 'FULL';
  }
  
  // 中等长度上下文
  if (contextTokens < 60000) {
    return 'VALUES';
  }
  
  // 上下文紧张 → 只用核心
  return 'CORE';
}

/**
 * 条件激活场景人格
 */
function getActivePersona(personality, context) {
  const conditions = personality.persona_conditions || {};
  
  for (const [name, cond] of Object.entries(conditions)) {
    if (evaluateCondition(cond, context)) {
      return name;
    }
  }
  
  return personality.active_persona || 'default';
}

/**
 * 评估条件
 */
function evaluateCondition(condition, context) {
  if (!condition) return false;
  
  // 检查关键词
  if (condition.keywords) {
    const text = (context.message || '').toLowerCase();
    return condition.keywords.some(kw => text.includes(kw.toLowerCase()));
  }
  
  // 检查时间条件
  if (condition.time_range) {
    const hour = new Date().getHours();
    const [start, end] = condition.time_range;
    return hour >= start && hour <= end;
  }
  
  return false;
}

/**
 * 添加场景人格
 */
function addPersonaCondition(personality, name, condition) {
  const updated = { ...personality };
  updated.persona_conditions = { ...updated.persona_conditions, [name]: condition };
  return updated;
}

/**
 * 从 Remelight 获取用户画像
 */
async function getUserProfile(workspace) {
  return new Promise((resolve) => {
    const scriptPath = path.join(__dirname, '..', '..', 'scripts', 'reme_session.py');
    const python = spawn('python', [scriptPath, 'profile', '--get'], {
      shell: true,
      cwd: workspace,
    });

    let stdout = '';
    let stderr = '';

    python.stdout.on('data', (data) => { stdout += data.toString(); });
    python.stderr.on('data', (data) => { stderr += data.toString(); });

    python.on('close', (code) => {
      if (code === 0) {
        try {
          resolve(JSON.parse(stdout));
        } catch {
          resolve(null);
        }
      } else {
        resolve(null);
      }
    });

    python.on('error', () => resolve(null));
  });
}

/**
 * 搜索历史会话
 */
async function searchHistory(workspace, query, days = 30) {
  return new Promise((resolve) => {
    const scriptPath = path.join(__dirname, '..', '..', 'scripts', 'reme_session.py');
    const python = spawn('python', [scriptPath, 'search', query, '--days', String(days), '--max-results', '3'], {
      shell: true,
      cwd: workspace,
    });

    let stdout = '';

    python.stdout.on('data', (data) => { stdout += data.toString(); });

    python.on('close', (code) => {
      if (code === 0) {
        try {
          resolve(JSON.parse(stdout));
        } catch {
          resolve([]);
        }
      } else {
        resolve([]);
      }
    });

    python.on('error', () => resolve([]));
  });
}

/**
 * 从对话学习用户偏好
 */
async function learnUserPreference(workspace, messages) {
  return new Promise((resolve) => {
    const scriptPath = path.join(__dirname, '..', '..', 'scripts', 'reme_session.py');
    const python = spawn('python', [scriptPath, 'profile', '--learn', JSON.stringify(messages)], {
      shell: true,
      cwd: workspace,
    });

    let stdout = '';

    python.stdout.on('data', (data) => { stdout += data.toString(); });

    python.on('close', (code) => {
      if (code === 0) {
        try {
          resolve(JSON.parse(stdout));
        } catch {
          resolve(null);
        }
      } else {
        resolve(null);
      }
    });

    python.on('error', () => resolve(null));
  });
}

/**
 * 加载持久化人格
 */
function loadPersonality() {
  try {
    if (fs.existsSync(PERSONALITY_FILE)) {
      const raw = fs.readFileSync(PERSONALITY_FILE, 'utf-8').trim();
      if (raw) return JSON.parse(raw);
    }
  } catch {}
  return createPersonality();
}

/**
 * 保存人格
 */
function savePersonality(personality) {
  const dir = path.dirname(PERSONALITY_FILE);
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(PERSONALITY_FILE, JSON.stringify(personality, null, 2) + '\n', 'utf-8', 'utf-8');
}

/**
 * 初始化 SOUL.md（如果不存在）
 */
function initSoul() {
  if (!fs.existsSync(SOUL_FILE)) {
    saveSoul(DEFAULT_SOUL);
    console.log('   📝 SOUL.md 已初始化');
  }
}

module.exports = {
  createPersonality,
  updatePersonality,
  suggestFromPersonality,
  loadPersonality,
  savePersonality,
  loadSoul,
  saveSoul,
  getSoulLevel,
  getActiveLevel,
  getActivePersona,
  addPersonaCondition,
  getUserProfile,
  searchHistory,
  learnUserPreference,
  initSoul,
  PERSONA_LEVELS,
  DEFAULT_SOUL,
};