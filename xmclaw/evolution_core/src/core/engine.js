'use strict';

/**
 * XM-AUTO-EVO 核心引擎
 *
 * 整合观察 → 学习 → 进化的完整自动循环。
 *
 * 集成了以下核心子系统：
 * - PCEC 周期管理（停滞检测 + 思维爆炸突破）
 * - VFM 评分器（Gene/节点的价值评估 + 自适应权重调整）
 * - 变异协议（repair/optimize/innovate 三类变异 + 策略过滤）
 * - 固化协议（验证 + ADL检查 + 提交/回滚闭环）
 * - 能力树修剪（自动清理长期不活跃节点）
 * - Gene 自动生成（从模式发现到能力节点）
 */

const path = require('path');
const fs = require('fs');

// ── 基础记忆模块 ─────────────────────────────────────
const ShortTermMemory = require('../memory/short');
const MediumTermMemory = require('../memory/medium');
const LongTermMemory = require('../memory/long');
const PatternMatcher = require('../observer/pattern');

// ── 信号 & 观察 ─────────────────────────────────────
const { extractSignals, extractFromSessions, getSignalStats, extractText, loadFromDialogDir } = require('../observer/signals');
const { CrossSessionAnalyzer } = require('../observer/cross_session');

// ── ADL 安全 ────────────────────────────────────────
const { detectDegradation } = require('../adl/validator');
const { createSnapshot, rollback } = require('../adl/rollback');

// ── GEP（Gene + Capsule + Event）────────────────────
const { rankGenes, selectGene } = require('../gep/selector');
const { loadGenes, addGene, addCapsule, appendEvent, loadEvents } = require('../gep/store');
const { createEvent } = require('../gep/event');
const { createMutation, checkStrategyAllowance, assessRisk } = require('../gep/mutation');

// ── VFM（价值函数）───────────────────────────────────
const { computeVScore, isWorthEvolving } = require('../vfm/scorer');
const { mutateWeights, getWeights } = require('../vfm/mutator');

// ── PCEC（周期管理）─────────────────────────────────
const { PCECCycle, getStagnantCount } = require('../pcec/cycle');
const { generateExplosion, extractFromExplosion } = require('../pcec/explosion');

// ── 能力生成 ────────────────────────────────────────
const { autoGenerateGene, autoCreateCapabilityNode } = require('../evolution/gene_forge');
const { executeCodePlan } = require('../evolution/code_generator');
const { repairSkill } = require('../evolution/error_repair');
const { autoCreateSkill } = require('../evolution/skill_maker');
const { CapabilityTree } = require('../evolution/tree');

// ── 能力树修剪 ──────────────────────────────────────
const { analyzePruning, pruneTree } = require('../tree/pruner');

// ── 固化协议 ────────────────────────────────────────
const { solidify } = require('./solidify');

// ── 策略 & 人格 ─────────────────────────────────────
const { getStrategy, autoDetectStrategy } = require('../strategy');
const { loadPersonality, updatePersonality, savePersonality, initSoul, getSoulLevel, getUserProfile, searchHistory, learnUserPreference } = require('../personality');

// ── Memory Bridge（进化结果持久化）─────────────────────
const memoryBridge = require('../memory_bridge');

// ── 向量记忆 ────────────────────────────────────────
const { initFromConfig } = require('../memory/vector');

// ── CoPaw 通知 ──────────────────────────────────────
const { CopawNotifier } = require('./notifier');

class AutoEvoEngine {
  constructor(workspace, config) {
    this.workspace = workspace;
    this.config = config;
    this.shortMemory = new ShortTermMemory(workspace);
    this.mediumMemory = new MediumTermMemory(workspace);
    this.longMemory = new LongTermMemory(workspace);
    this.patternMatcher = new PatternMatcher(config);
    this.capabilityTree = new CapabilityTree();
    
    // 初始化 skill_maker 的工作区路径
    const { setWorkspace } = require('../evolution/skill_maker');
    setWorkspace(workspace);
    this.personality = loadPersonality();
    this.initialized = false;
    this.cycleCount = 0;
    this._lastSignals = [];
    this._lastInsights = [];
    // PCEC 周期实例
    this._pcecCycle = null;
    // 每周修剪计数
    this._lastPruneWeek = this._getWeekNumber();
    // CoPaw 通知器
    this.notifier = new CopawNotifier(workspace);
  }

  _getWeekNumber() {
    const now = new Date();
    const start = new Date(now.getFullYear(), 0, 1);
    return Math.ceil(((now - start) / 86400000 + start.getDay() + 1) / 7);
  }

  async init() {
    if (this.initialized) return;
    initFromConfig(this.config);
    await this.shortMemory.init();
    await this.mediumMemory.init();
    await this.longMemory.init();
    this.initialized = true;

    // 初始化 SOUL.md
    initSoul();

    // 加载用户画像
    try {
      const profile = await getUserProfile(this.workspace);
      if (profile) {
        this.personality.user_profile = profile;
        console.log(`   👤 用户画像: ${profile.name || '未知'}`);
      }
    } catch (e) {
      console.log('   👤 用户画像: 未加载');
    }

    // 加载 MEMORY.md 作为跨会话上下文
    try {
      const memoryPath = path.join(this.workspace, 'MEMORY.md');
      if (fs.existsSync(memoryPath)) {
        const memoryContent = fs.readFileSync(memoryPath, 'utf-8');
        this.personality.memory_context = memoryContent.slice(0, 5000);
        const lessonCount = (memoryContent.match(/lesson|教训|经验/gi) || []).length;
        console.log(`   📚 MEMORY.md 加载成功（约 ${lessonCount} 条教训）`);
      }
    } catch (e) {
      console.log('   📚 MEMORY.md: 未加载');
    }

    console.log('   📦 向量记忆: CoPaw ReMeLight 集成模式');

    // 启动新 PCEC 周期
    this._pcecCycle = new PCECCycle();
    console.log(`   🔄 PCEC 周期: ${this._pcecCycle.id}`);
    console.log('   ✅ XM-AUTO-EVO 引擎初始化完成');
  }

  // ── 1. 观察阶段 ──────────────────────────────────────

  async observe() {
    await this.init();
    this.cycleCount++;

    console.log(`\n🔍 观察阶段 [#${this.cycleCount}]`);
    console.log('─'.repeat(40));

    const dialogDir = path.join(this.workspace, 'dialog');
    const sessionsDir = path.join(this.workspace, 'sessions');

    // 从 dialog 提取信号
    const signals = extractSignals(this.workspace);
    const stats = getSignalStats(signals);

    if (signals.length > 0) {
      console.log(`   对话条目: ${stats.total} 条`);
      console.log(`   提取信号: ${signals.length} 个（去重后 ${stats.unique} 种）`);
      if (stats.topTools && stats.topTools.length > 0) {
        console.log(`   高频工具: ${stats.topTools.slice(0, 3).map(([k]) => k.replace('tool_use:', '')).join(', ')}`);
      }
      if (stats.topIntents && stats.topIntents.length > 0) {
        console.log(`   用户意图: ${stats.topIntents.slice(0, 3).map(([k]) => k.replace('intent:', '')).join(', ')}`);
      }
    } else {
      console.log(`   对话条目: 0 条（对话目录: ${dialogDir}）`);
    }

    // 从 dialog 条目提取用户消息模式
    const dialogEntries = loadFromDialogDir(dialogDir, 7, 500);
    const dialogUserTexts = dialogEntries
      .filter(e => e.role === 'user')
      .map(e => extractText(e.content || []))
      .filter(Boolean);

    const sessionEntries = await extractFromSessions(sessionsDir, 100);
    const sessionUserTexts = sessionEntries
      .filter(e => e.role === 'user')
      .map(e => extractText(e.content || []))
      .filter(Boolean);

    const allUserTexts = [...dialogUserTexts, ...sessionUserTexts];
    console.log(`   用户消息: ${dialogUserTexts.length} 条（含 session ${sessionUserTexts.length} 条）`);

    const patterns = this.patternMatcher.extractPatterns(allUserTexts);
    const repeatingPatterns = this.patternMatcher.detectRepeatingPatterns(patterns);

    for (const pattern of repeatingPatterns) {
      await this.mediumMemory.recordPattern(pattern);
    }

    // 🔀 跨会话分析
    const crossSessionAnalyzer = new CrossSessionAnalyzer(this.workspace);
    const crossSessionResult = crossSessionAnalyzer.analyze(50);
    if (crossSessionResult.patterns.length > 0 || crossSessionResult.insights.length > 0) {
      console.log(`   跨会话分析: ${crossSessionResult.sessionsAnalyzed} 个会话, ${crossSessionResult.patterns.length} 个模式, ${crossSessionResult.insights.length} 条洞察`);
      for (const pattern of crossSessionResult.patterns) {
        patterns.push(pattern);
        await this.mediumMemory.recordPattern(pattern);
      }
      if (crossSessionResult.insights.length > 0) {
        this._lastInsights = this._lastInsights || [];
        this._lastInsights.push(...crossSessionResult.insights);
      }
    }

    // 记忆上下文加载（通过直接读取 MEMORY.md，ReMeLight 负责索引）
    if (signals.length > 0) {
      // 会话历史搜索
      try {
        const topSignal = signals[0] || '';
        const history = await searchHistory(this.workspace, topSignal.slice(0, 50), 30);
        if (history && history.length > 0) {
          console.log(`   📜 历史会话命中: ${history.length} 条`);
        }
      } catch (e) {
        // 会话搜索可选，失败不影响主流程
      }
    }

    this._lastSignals = signals;
    this._lastCrossSessionPatterns = crossSessionResult.patterns || [];
    appendEvent(createEvent({
      event_type: 'observe_complete',
      payload: { signals, patterns: patterns.length, repeating: repeatingPatterns.length, crossSession: crossSessionResult.patterns.length },
    }));

    console.log(`   提取到 ${patterns.length} 个模式, ${repeatingPatterns.length} 个重复模式`);
    return { signals, patterns, repeatingPatterns };
  }

  // ── 2. 学习阶段 ──────────────────────────────────────

  async learn() {
    await this.init();

    console.log('\n🧠 学习阶段');
    console.log('─'.repeat(40));

    const trends = this.mediumMemory.detectTrends ?
      await this.mediumMemory.detectTrends(7) : [];
    console.log(`   检测到 ${trends.length} 个趋势`);

    const summary = this.mediumMemory.generateSummary ?
      await this.mediumMemory.generateSummary(7) : { insights: [] };
    console.log(`   生成 ${summary.insights?.length || 0} 条洞察`);

    // 保存洞察到 engine 实例，供进化阶段使用
    this._lastInsights = summary.insights || [];

    if (summary.insights && summary.insights.length > 0 && this.longMemory.addInsight) {
      for (const insight of summary.insights) {
        await this.longMemory.addInsight(insight);
      }
    }

    // 同步到长时记忆
    if (summary.insights?.length > 0) {
      await this.longMemory.syncFromMediumTerm(summary);
    }

    // 学习用户偏好（从最近对话）
    try {
      const dialogDir = path.join(this.workspace, 'dialog');
      const recentMessages = loadFromDialogDir(dialogDir, 3, 50)
        .filter(e => e.role === 'user')
        .map(e => extractText(e.content || ''))
        .filter(Boolean);

      if (recentMessages.length > 0) {
        const learned = await learnUserPreference(this.workspace, recentMessages);
        if (learned) {
          console.log(`   📊 用户偏好学习: ${Object.keys(learned).length} 个特征`);
          // 自动记录到 memory
          for (const [key, value] of Object.entries(learned)) {
            await memoryBridge.recordUserPreference(`${key}: ${value}`, this.workspace);
          }
        }
      }
    } catch (e) {
      // 用户偏好学习可选
    }

    // 将洞察写入今日日志
    try {
      await this.mediumMemory.writeInsightsToDailyLog(this.workspace);
    } catch (e) {
      console.log(`   ⚠️ 洞察写入日志失败: ${e.message}`);
    }

    appendEvent(createEvent({
      event_type: 'learn_complete',
      payload: { trends: trends.length, insights: summary.insights?.length || 0 },
    }));

    return { trends, summary };
  }

  // ── 3. 进化阶段（完整 PCEC + VFM 闭环） ─────────────

  async evolve() {
    await this.init();

    console.log('\n⚙️ 进化阶段');
    console.log('─'.repeat(40));

    const config = this.config.auto_evolution;
    if (!config.enabled) {
      console.log('   ⏸️ 自动进化已禁用');
      return { evolved: false, reason: 'disabled' };
    }

    // ── 进化前创建 Snapshot ────────────────────
    const snapshot = createSnapshot(this.workspace);
    if (snapshot.success && snapshot.snapshot) {
      console.log(`   📸 创建快照: ${snapshot.snapshot.slice(0, 8)}...`);
    } else {
      console.log('   📸 创建快照: 无变更或 git 不可用');
    }

    // ── ADL 劣化检查 ──────────────────────────────
    const capsules = require('../gep/store').loadCapsules();
    const events = loadEvents();
    const degradation = detectDegradation(capsules, events);

    if (degradation.degraded) {
      console.log(`   ⚠️ 检测到劣化: ${degradation.indicators.join(', ')}`);
      console.log(`   💡 ${degradation.recommendation}`);
      appendEvent(createEvent({ event_type: 'adl_degradation_detected', payload: degradation }));
      this._pcecCycle?.addOutcome({ type: 'capability', description: 'ADL 触发安全回滚' });
      return { evolved: false, reason: 'adl_degradation', ...degradation };
    }

    const signals = this._lastSignals || [];

    // ── 确定策略 & 人格建议 ───────────────────────
    const strategyName = autoDetectStrategy(events);
    const strategy = getStrategy(strategyName);
    console.log(`   🎯 策略: ${strategyName}`);
    console.log(`   📊 VFM 权重: ${JSON.stringify(getWeights())}`);

    const patterns = this.patternMatcher.getRepeatingPatterns() || [];
    const crossSessionPatterns = this._lastCrossSessionPatterns || [];
    // 从学习阶段获取洞察
    const insights = this._lastInsights || [];
    const threshold = config.evolution_trigger_threshold || 3;

    const evolved = [];

    // ── 路径 0：从洞察直接触发进化 ────────────────
    if (insights.length > 0) {
      console.log(`   💡 从洞察触发进化: ${insights.length} 条`);
      for (const insight of insights.slice(0, 2)) {
        if (insight.type === 'capability_gap' || insight.type === 'user_request') {
          const pattern = {
            category: insight.title?.replace(/\s+/g, '_').toLowerCase() || 'insight_driven',
            signature: `${insight.type}:${insight.title}`,
            description: insight.description,
            confidence: insight.priority === 'high' ? 0.9 : 0.7,
            type: 'insight',
          };

          if (!this._geneExistsForPattern(pattern)) {
            const gene = autoGenerateGene(pattern, config);
            if (!gene) continue;

            const vfmScore = gene.v_score ?? computeVScore(gene);
            const vfmWorth = isWorthEvolving({ ...gene, v_score: vfmScore });
            console.log(`   📈 VFM 评分: ${vfmScore}/100（阈值 ${vfmWorth.threshold}）`);

            if (!vfmWorth.worth) {
              console.log(`   ⏭️ VFM 评分不足，跳过此洞察`);
              continue;
            }

            const mutation = createMutation({
              category: 'innovate',
              trigger_signals: signals.slice(0, 5),
              target: `洞察: ${insight.title}`,
              expected_effect: insight.suggestion || '增强对应能力',
              gene_id: gene.id,
            });

            await this._applyGene(gene, mutation, strategy, evolved, 'insight');
          }
        }

        if (insight.type === 'quality_issue') {
          const pattern = {
            category: 'repair_driven',
            signature: 'quality_issue',
            description: insight.description,
            confidence: 0.85,
            type: 'insight',
          };

          if (!this._geneExistsForPattern(pattern)) {
            const gene = autoGenerateGene(pattern, config);
            if (!gene) continue;

            const vfmScore = gene.v_score ?? computeVScore(gene);
            const vfmWorth = isWorthEvolving({ ...gene, v_score: vfmScore });
            if (!vfmWorth.worth) continue;

            const mutation = createMutation({
              category: 'repair',
              trigger_signals: signals.slice(0, 5),
              target: `修复: ${insight.title}`,
              expected_effect: '修复质量问题',
              gene_id: gene.id,
            });

            await this._applyGene(gene, mutation, strategy, evolved, 'insight_repair');
          }
        }
      }
    }

    // ── 路径 A：从模式自动生成 Gene/Skill ─────────
    if (config.auto_gene_creation && patterns.length >= threshold) {
      for (const pattern of patterns.slice(0, 2)) {
        if (!this._geneExistsForPattern(pattern)) {
          // 生成 Gene
          const gene = autoGenerateGene(pattern, config);
          if (!gene) continue;

          // 创建变异提案（必须在 gene 生成后，才能传入 gene_id）
          const mutation = createMutation({
            category: pattern.category === 'innovate' ? 'innovate' :
              (pattern.confidence > 0.8 ? 'optimize' : 'repair'),
            trigger_signals: signals.slice(0, 5),
            target: `模式: ${pattern.signature || pattern.category || 'unknown'}`,
            expected_effect: `增强 ${pattern.category || 'unknown'} 能力`,
            gene_id: gene.id,
          });

          // 策略允许检查
          const allowCheck = checkStrategyAllowance(mutation.category, strategy, []);
          if (!allowCheck.allowed) {
            console.log(`   ⏭️ 策略禁止 ${mutation.category} 变异: ${allowCheck.reason}`);
            continue;
          }

          // VFM 评分
          const vfmScore = gene.v_score ?? computeVScore(gene);
          const vfmWorth = isWorthEvolving({ ...gene, v_score: vfmScore });
          console.log(`   📈 VFM 评分: ${vfmScore}/100（阈值 ${vfmWorth.threshold}）`);

          if (!vfmWorth.worth) {
            console.log(`   ⏭️ VFM 评分不足，跳过此模式`);
            continue;
          }

          // 应用 Gene（生成代码 + 固化）
          await this._applyGene(gene, mutation, strategy, evolved, 'pattern');
        }
      }
    }

    // ── 路径 B：自动创建 Skill ─────────────────────
    const allSkillPatterns = [...crossSessionPatterns, ...patterns];
    if (config.auto_skill_creation && allSkillPatterns.length >= threshold) {
      // 优先选择跨会话的 innovate/repair 模式创建新 Skill，避免总是迭代同一个 optimize 模式
      const skillCandidates = allSkillPatterns.filter(p => p.cross_session && (p.category === 'innovate' || p.category === 'repair'));
      const fallbackCandidates = allSkillPatterns.filter(p => p.cross_session);
      const candidatePatterns = [...skillCandidates, ...fallbackCandidates, ...allSkillPatterns];

      // 基于 signature + session_count 双重去重
      const skillJsonPath = path.join(this.workspace, 'skill.json');
      let skillJson = { skills: {} };
      try {
        skillJson = JSON.parse(fs.readFileSync(skillJsonPath, 'utf-8'));
      } catch {}
      const autoSkillEntries = Object.entries(skillJson.skills || {})
        .filter(([k, v]) => k.startsWith('auto_'));

      // 构建已存在的 signature + session_count 组合集合
      const existingCombinations = new Set();
      autoSkillEntries.forEach(([k, v]) => {
        const sig = v.metadata?.signature;
        if (sig) {
          // 提取 session_count 从 description 中（格式：影响 XX 个会话）
          const desc = v.metadata?.description || '';
          const match = desc.match(/影响 (\d+) 个会话/);
          const sessionCount = match ? parseInt(match[1]) : null;
          
          if (sessionCount) {
            existingCombinations.add(`${sig}:${sessionCount}`);
          } else {
            existingCombinations.add(sig);
          }
        }
      });

      let skillCreated = false;
      for (const chosenPattern of candidatePatterns) {
        // 对于跨会话 repair 模式，使用 signature + session_count 组合来唯一标识
        const sig = chosenPattern.signature || '';
        const sessionCount = chosenPattern.cross_session && chosenPattern.category === 'repair' 
          ? chosenPattern.session_count 
          : null;
        
        const combinationKey = sessionCount ? `${sig}:${sessionCount}` : sig;
        const combinationExists = existingCombinations.has(combinationKey);

        // 也保留原有的 signature 单独检查（兼容非跨会话模式）
        const signatureOnly = sig && existingCombinations.has(sig);
        
        if (combinationExists || (sessionCount === null && signatureOnly)) {
          const reason = sessionCount 
            ? `signature + session_count 组合已存在 (${combinationKey})` 
            : `signature 已存在 (${sig.substring(0, 40)})`;
          console.log(`   🛑 跳过创建: ${reason}`);
          continue;
        }

        const autoSkill = autoCreateSkill(chosenPattern);
        if (autoSkill && !autoSkill.existed) {
          evolved.push({ type: 'skill', id: autoSkill.skillId });
          this._pcecCycle?.addOutcome({ type: 'skill', description: `Skill: ${autoSkill.skillId}` });
          await memoryBridge.recordSkillCreated(autoSkill, this.workspace);
          skillCreated = true;
          console.log(`   ✅ 自动创建 Skill 成功: ${autoSkill.skillId}`);
          break; // 成功创建一个就退出
        }
      }

      if (!skillCreated) {
        console.log(`   ⏭️ 所有候选 Skill 均已存在或条件不满足，跳过创建`);
      }
    }

    // ── 记录 Capsule ─────────────────────────────
    // 只要有任何进化尝试（Gene 或 Skill），就记录 Capsule
    if (evolved.length > 0) {
      const geneCapsules = evolved.filter(e => e.type === 'gene' && e.capsule);
      const skillItems = evolved.filter(e => e.type === 'skill');
      addCapsule({
        type: 'Capsule',
        id: `capsule_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`,
        success: true,
        patterns_detected: patterns.length,
        genes_created: geneCapsules.length,
        skills_created: skillItems.length,
        mutation_category: geneCapsules[0]?.capsule?.mutation_category || (skillItems.length > 0 ? 'innovate' : null),
        timestamp: new Date().toISOString(),
      });
    }

    // ── 结束 PCEC 周期 ────────────────────────────
    if (this._pcecCycle) {
      const pcecResult = this._pcecCycle.complete();
      console.log(`   🔄 PCEC 周期: ${pcecResult.substantive ? '有实质产出 ✅' : '停滞 ⚠️'}（连续停滞: ${pcecResult.stagnant_count} 次）`);

      if (pcecResult.stagnant_count >= 2) {
        // 触发思维爆炸
        console.log('   💥 触发思维爆炸（连续停滞）');
        const explosion = this.generateExplosionPrompt();
        console.log(`   💭 爆炸 prompt 生成完成（${explosion.questions.length} 个问题）`);
        this._pcecCycle?.addOutcome({ type: 'abstraction', description: `爆炸: ${explosion.focusArea}` });

        // 将思维爆炸问题写入 memory 文件
        this._recordThoughtExplosion(explosion);
      }

      // 开启新周期
      this._pcecCycle = new PCECCycle();
      console.log(`   🔄 新 PCEC 周期: ${this._pcecCycle.id}`);
    }

    appendEvent(createEvent({
      event_type: 'evolution_complete',
      payload: { evolved: evolved.length > 0, items: evolved },
    }));

    // ── 更新人格 & VFM 权重 ───────────────────────
    if (evolved.length > 0) {
      console.log(`   ✅ 进化完成! 创建了 ${evolved.length} 个新能力`);
      this.personality = updatePersonality(this.personality, {
        success: true,
        category: evolved[0].type === 'gene' ? 'optimize' : 'innovate',
      });
      savePersonality(this.personality);
      // 进化成功 → 微调 VFM 权重
      mutateWeights();
      console.log(`   🧠 人格更新: mood=${this.personality.mood} confidence=${this.personality.confidence}`);
      console.log(`   📊 VFM 新权重: ${JSON.stringify(getWeights())}`);
    } else {
      console.log('   ⏭️ 本轮无进化（条件未满足或能力已存在）');
      // 无进化 → 记录到 PCEC 供停滞判断
      this._pcecCycle?.addOutcome({ type: 'capability', description: '本轮无实质进化' });
    }

    // ── 每周能力树修剪 ───────────────────────────
    const currentWeek = this._getWeekNumber();
    if (currentWeek !== this._lastPruneWeek) {
      console.log('\n🌿 执行每周能力树修剪...');
      const pruneResult = pruneTree(this.capabilityTree, false);
      console.log(`   修剪: ${pruneResult.auto_pruned.length} 个长期不活跃节点`);
      this._lastPruneWeek = currentWeek;
    }

    const result = { evolved: evolved.length > 0, items: evolved };

    // ── CoPaw 主动通知 ────────────────────────────
    if (result.evolved) {
      this.notifier.notifyEvolution(result);
    }

    // ── 写入进化结果摘要（供 CoPaw 读取）───────────
    try {
      const summaryPath = path.join(this.workspace, 'skills', 'xm-auto-evo', 'data', 'last_evolution.json');
      fs.mkdirSync(path.dirname(summaryPath), { recursive: true });
      fs.writeFileSync(summaryPath, JSON.stringify({
        timestamp: new Date().toISOString(),
        evolved: result.evolved,
        items: result.items.map(i => ({ type: i.type, id: i.id })),
        pcecCycle: this._pcecCycle?.id,
      }, null, 2), 'utf-8');
    } catch {}

    return result;
  }

  _geneExistsForPattern(pattern) {
    const genes = loadGenes();
    const sig = pattern.signature || '';
    const cat = pattern.category || '';
    return genes.some(g => {
      const signals = g.signals_match || [];
      // 精确匹配 signature 或 category
      if (sig && (signals.includes(sig) || g.id === sig || g.category === sig)) return true;
      if (cat && (signals.includes(cat) || g.category === cat)) return true;
      return false;
    });
  }

  /**
   * 统一应用 Gene：生成代码 → 固化 → 记录
   */
  async _applyGene(gene, mutation, strategy, evolved, source = 'pattern') {
    const allowCheck = checkStrategyAllowance(mutation.category, strategy, []);
    if (!allowCheck.allowed) {
      console.log(`   ⏭️ 策略禁止 ${mutation.category} 变异: ${allowCheck.reason}`);
      return false;
    }

    // 1. 生成实际代码
    const codeResult = executeCodePlan(gene, this.workspace);
    if (!codeResult.success) {
      console.log(`   ❌ 代码生成失败: ${codeResult.error}`);
      return false;
    }

    // 2. 固化
    const solidifyResult = await solidify({
      gene,
      mutation,
      changedFiles: codeResult.changedFiles || [],
      newFiles: codeResult.newFiles || [],
      dryRun: false,
      cwd: this.workspace,
    });

    const vfmScore = gene.v_score ?? computeVScore(gene);

    if (solidifyResult.success) {
      autoCreateCapabilityNode({ signature: gene.signals_match[0], category: gene.category, confidence: 0.7 }, this.capabilityTree);
      evolved.push({ type: 'gene', id: gene.id, vfmScore, source, capsule: solidifyResult.capsule });
      this._pcecCycle?.addOutcome({ type: 'capability', description: `Gene: ${gene.id}` });
      await memoryBridge.recordGeneCreated({ id: gene.id, vfmScore }, this.workspace);
      console.log(`   ✅ Gene 固化成功: ${gene.id}`);
      return true;
    } else {
      const reason = solidifyResult.validation?.passed === false ? '验证失败' : 'ADL阻断';
      console.log(`   ❌ 固化失败: ${reason}`);

      // 记录详细错误到日志，供错误驱动修复使用
      let errorOutput = '';
      if (solidifyResult.validation?.passed === false && solidifyResult.validation.results) {
        for (const r of solidifyResult.validation.results) {
          if (!r.success) {
            console.log(`      🔴 ${r.command}`);
            console.log(`      📛 ${r.output?.slice(0, 200) || '无输出'}`);
            errorOutput += r.output + '\n';
          }
        }
      }

      // 尝试错误驱动修复（仅针对 validation 失败）
      if (reason === '验证失败' && errorOutput) {
        const { findSkillDirs } = require('../evolution/code_generator');
        let skillDirs = findSkillDirs
          ? findSkillDirs(gene.signals_match[0].replace(/^auto_/, '').replace(/:.*$/, ''), this.workspace)
          : [];
        if (skillDirs.length === 0) {
          // 尝试从 files_to_modify 找 target
          const target = gene.files_to_modify?.[0]?.target;
          skillDirs = target ? findSkillDirs(target, this.workspace) : [];
        }
        if (skillDirs.length > 0) {
          const latestDir = skillDirs[skillDirs.length - 1];
          const skillPath = path.join(this.workspace, 'skills', latestDir);
          console.log(`   🔧 尝试自动修复 Skill: ${latestDir}`);
          const repairResult = repairSkill(skillPath, errorOutput);
          if (repairResult.success) {
            console.log(`   ✅ 自动修复成功: ${repairResult.fixes.join(', ')}`);
            // 重新验证
            const retryResult = await solidify({
              gene,
              mutation,
              changedFiles: codeResult.changedFiles || [],
              newFiles: codeResult.newFiles || [],
              dryRun: false,
              cwd: this.workspace,
            });
            if (retryResult.success) {
              autoCreateCapabilityNode({ signature: gene.signals_match[0], category: gene.category, confidence: 0.7 }, this.capabilityTree);
              evolved.push({ type: 'gene', id: gene.id, vfmScore, source: `${source}_repaired`, capsule: retryResult.capsule });
              this._pcecCycle?.addOutcome({ type: 'capability', description: `Gene: ${gene.id} (repaired)` });
              await memoryBridge.recordGeneCreated({ id: gene.id, vfmScore }, this.workspace);
              console.log(`   ✅ 修复后 Gene 固化成功: ${gene.id}`);
              return true;
            } else {
              console.log(`   ❌ 修复后仍然失败`);
            }
          } else {
            console.log(`   ⚠️ 自动修复未生效: ${repairResult.fixes.join(', ')}`);
          }
        }
      }

      // 将失败信息写入今日日志，加速下次 repair
      try {
        const fs = require('node:fs');
        const today = new Date().toISOString().split('T')[0];
        const logFile = path.join(this.workspace, 'memory', `${today}.md`);
        const entry = `\n### ${new Date().toLocaleTimeString()} Gene 固化失败\n- Gene: ${gene.id}\n- 原因: ${reason}\n- 详情: ${JSON.stringify(solidifyResult.validation?.results || [])}\n`;
        fs.appendFileSync(logFile, entry, 'utf-8');
      } catch {}

      return false;
    }
  }

  // ── 生成思维爆炸 Prompt ──────────────────────────

  generateExplosionPrompt() {
    const stagnantCycles = getStagnantCount();
    const recentEvents = loadEvents().slice(-20);
    const recentFailures = recentEvents
      .filter(e => e.event_type === 'solidify_failed')
      .map(e => e.payload?.reason || 'unknown');
    const patterns = this.patternMatcher.getRepeatingPatterns() || [];
    const capabilities = this.capabilityTree.getActiveNodes().map(n => n.name);

    const explosion = generateExplosion({
      currentCapabilities: capabilities,
      recentFailures,
      stagnantCycles,
      recentSignals: this._lastSignals.slice(0, 5),
    });

    return explosion;
  }

  // ── 记录思维爆炸到 memory ──────────────────────────

  async _recordThoughtExplosion(explosion) {
    const fs = require('node:fs');
    const today = new Date().toISOString().split('T')[0];
    const memoryFile = path.join(this.workspace, 'memory', `${today}.md`);

    const content = `
## 思维爆炸 [${new Date().toLocaleTimeString()}]
- 聚焦领域: ${explosion.focusArea}
- 问题数量: ${explosion.questions.length}

### 探索问题
${explosion.questions.map((q, i) => `${i + 1}. ${q}`).join('\n')}

### 待回答
（这些问题需要 agent 回答并执行相应行动）
`;

    try {
      const dir = path.dirname(memoryFile);
      if (!fs.existsSync(dir)) {
        fs.mkdirSync(dir, { recursive: true });
      }
      if (fs.existsSync(memoryFile)) {
        fs.appendFileSync(memoryFile, content, 'utf-8');
      } else {
        fs.writeFileSync(memoryFile, `# ${today} 每日笔记\n${content}\n`, 'utf-8');
      }
      console.log(`   📝 思维爆炸已记录到 ${memoryFile}`);
    } catch (e) {
      console.log(`   ⚠️ 思维爆炸记录失败: ${e.message}`);
    }
  }

  // ── 4. 完整周期（观察+学习+进化） ──────────────────────

  async runCycle() {
    await this.observe();
    await this.learn();
    return await this.evolve();
  }

  // ── 5. 生成进化建议 ──────────────────────────────────

  async generateEvolutionSuggestions() {
    await this.init();
    const signals = this._lastSignals || [];
    const patterns = this.patternMatcher.getRepeatingPatterns() || [];
    const ranked = rankGenes(signals);
    const strategy = autoDetectStrategy(loadEvents(), 'utf-8');
    const treeStats = this.capabilityTree.getStats();
    const weights = getWeights();
    const stagnant = getStagnantCount();
    const { suggestFromPersonality } = require('../personality');

    const suggestions = [];

    // 从人格状态获取建议
    const personaHint = suggestFromPersonality(this.personality);
    if (personaHint.preferCategory) {
      suggestions.push({
        priority: 'medium',
        type: 'persona_driven',
        description: `人格偏好驱动: ${personaHint.preferCategory}（${this.personality.mood} 心情）`,
      });
    }

    // 建议 1：VFM 高分 Gene
    if (ranked.length > 0) {
      suggestions.push({
        priority: 'high',
        type: 'gene_selection',
        description: `VFM 最高 Gene: ${ranked[0].id}（score: ${ranked[0].v_score}）`,
      });
    }

    // 建议 2：模式驱动生成
    if (patterns.length >= 1) {
      suggestions.push({
        priority: 'medium',
        type: 'auto_generation',
        description: `可从 ${patterns.length} 个重复模式生成新 Gene`,
      });
    }

    // 建议 3：能力树修剪
    if (treeStats.totalNodes > 5) {
      const { candidate_prune, auto_prune } = analyzePruning(this.capabilityTree.getAllNodes());
      if (auto_prune.length + candidate_prune.length > 0) {
        suggestions.push({
          priority: 'low',
          type: 'pruning',
          description: `可修剪 ${auto_prune.length + candidate_prune.length} 个不活跃节点`,
        });
      }
    }

    // 建议 4：停滞爆炸
    if (stagnant >= 2) {
      const explosion = this.generateExplosionPrompt();
      suggestions.push({
        priority: 'high',
        type: 'explosion',
        description: `连续 ${stagnant} 次无实质产出，建议运行思维爆炸`,
        prompt: explosion.prompt,
      });
    }

    return {
      signals: signals.length,
      patterns: patterns.length,
      strategy,
      vfmWeights: weights,
      stagnantCycles: stagnant,
      treeStats,
      suggestions,
    };
  }

  // ── 获取 PCEC 周期状态 ───────────────────────────

  getPCECStatus() {
    return this._pcecCycle?.getSummary() || { status: 'no_cycle' };
  }

  // ── 获取系统完整状态 ───────────────────────────

  getStatus() {
    const genes = loadGenes();
    const capsules = require('../gep/store').loadCapsules();
    const events = loadEvents();
    const { summarizeEvents } = require('../gep/event');
    const weights = getWeights();
    const tree = this.capabilityTree;

    return {
      initialized: this.initialized,
      cycleCount: this.cycleCount,
      genes: genes.length,
      capsules: capsules.length,
      events: events.length,
      eventSummary: summarizeEvents(events),
      capabilityTree: {
        totalNodes: tree ? Object.keys(tree.data?.nodes || {}).length : 0,
        rootChildren: tree?.data?.root?.children?.length || 0,
      },
      personality: this.personality,
      vfmWeights: weights,
      pcec: this._pcecCycle?.getSummary() || null,
      ollama: null, // checked at init time
    };
  }

  // ── 获取记忆系统状态 ───────────────────────────

  async getMemoryStatus() {
    await this.init();
    const short = this.shortMemory.getStats ? this.shortMemory.getStats() : {};
    // medium.getDailyStats() 是 medium 的实际方法，getStats 不存在
    const medium = this.mediumMemory.getDailyStats
      ? await this.mediumMemory.getDailyStats()
      : {};
    const long = this.longMemory.getStats ? await this.longMemory.getStats() : {};
    const patterns = this.patternMatcher?.getRepeatingPatterns?.() || [];

    // ReMeLight 监控的文件统计
    let remeFiles = 0;
    try {
      const fs = require('fs');
      const path = require('path');
      const memDir = path.join(this.workspace, 'memory');
      if (fs.existsSync(memDir)) {
        remeFiles = fs.readdirSync(memDir).filter(f => f.endsWith('.md')).length;
      }
      if (fs.existsSync(path.join(this.workspace, 'MEMORY.md'))) {
        remeFiles += 1;
      }
    } catch {}

    return {
      shortTerm: short,
      mediumTerm: medium,
      longTerm: long,
      repeatingPatterns: patterns.length,
      reme: {
        available: true,
        description: 'CoPaw ReMeLight 向量记忆系统',
        monitoredFiles: remeFiles,
      },
    };
  }
}

module.exports = AutoEvoEngine;
