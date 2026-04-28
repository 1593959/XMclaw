#!/usr/bin/env node
'use strict';

/**
 * XM-AUTO-EVO CLI 入口
 * 
 * 完整版 - 支持 start/observe/learn/evolve/suggest/status/memory 等命令
 */

const path = require('path');
const fsSync = require('fs');
const fs = require('fs').promises;

function findWorkspaceRoot(startDir) {
  let dir = path.resolve(startDir);
  for (let i = 0; i < 5; i++) {
    const hasDialog = fsSync.existsSync(path.join(dir, 'dialog'));
    const hasSessions = fsSync.existsSync(path.join(dir, 'sessions'));
    // 优先匹配包含 dialog/ 或 sessions/ 的目录（CoPaw 工作区特征）
    if (hasDialog || hasSessions) {
      return dir;
    }
    const parent = path.dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  // 兜底：如果都没有，再尝试用 MEMORY.md
  dir = path.resolve(startDir);
  for (let i = 0; i < 5; i++) {
    if (fsSync.existsSync(path.join(dir, 'MEMORY.md'))) return dir;
    const parent = path.dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return path.resolve(startDir);
}

// 工作区路径（支持通过参数传入，或自动向上查找）
const WORKSPACE = process.env.WORKSPACE || findWorkspaceRoot(process.cwd());

// 加载配置
async function loadConfig() {
  const configPath = path.join(__dirname, 'config', 'auto-evo.json');
  try {
    const content = await fs.readFile(configPath, 'utf-8');
    return JSON.parse(content);
  } catch (e) {
    console.log('⚠️ 配置文件不存在，使用默认配置');
    return getDefaultConfig();
  }
}

function getDefaultConfig() {
  return {
    auto_evolution: {
      enabled: true,
      observation_interval_min: 30,
      evolution_trigger_threshold: 3,
      auto_gene_creation: true,
      auto_skill_creation: true,
      auto_memory_update: true,
      safety: {
        require_validation: true,
        rollback_on_regression: true,
        max_genes_per_day: 5,
        v_score_threshold: 50,
      },
    },
    pattern_detection: {
      min_occurrences: 3,
      similarity_threshold: 0.7,
      forbidden_patterns: ['password', 'secret', 'key', 'token', 'api_key'],
    },
    memory: {
      short_term: { max_entries: 50, ttl_hours: 24 },
      medium_term: { auto_summarize: true, max_entries: 100 },
      long_term: { auto_update: true },
    },
    vector: {
      ollama_base: 'http://localhost:11434',
      model: 'qwen3-embedding:0.6b',
      enabled: true,
    },
    scheduler: {
      interval_min: 30,
      min_interval_min: 10,
      max_interval_min: 240,
    },
  };
}

// 加载引擎
async function loadEngine() {
  const config = await loadConfig();
  const AutoEvoEngine = require('./src/core/engine');
  return new AutoEvoEngine(WORKSPACE, config);
}

// 命令处理
const command = process.argv[2];
const args = process.argv.slice(3);

async function main() {
  console.log('\n🧬 XM-AUTO-EVO - 完全自动进化系统');
  console.log('═'.repeat(50));
  console.log(`📁 工作区: ${WORKSPACE}`);
  console.log(`⏰ 启动时间: ${new Date().toLocaleString('zh-CN')}`);

  const engine = await loadEngine();

  switch (command) {
    case 'start':
      // 启动完整进化循环
      await engine.init();
      console.log('\n🚀 启动完整进化循环...');
      const result = await engine.runCycle();
      console.log('\n' + '═'.repeat(50));
      if (result.evolved) {
        const items = result.items || [];
        const geneCount = items.filter(i => i.type === 'gene').length;
        const skillCount = items.filter(i => i.type === 'skill').length;
        console.log('✅ 进化完成!');
        if (geneCount > 0) console.log(`   🧬 新 Gene: ${geneCount} 个`);
        if (skillCount > 0) console.log(`   🛠️  新 Skill: ${skillCount} 个`);
        if (items.length > 0) {
          console.log(`   详细: ${items.map(i => `${i.type}:${i.id}`).join(', ')}`);
        }
      } else {
        console.log(`⏭️ 本轮无进化 (${result.reason || '条件未满足或能力已存在'})`);
      }
      break;

    case 'observe':
      await engine.init();
      const obs = await engine.observe();
      console.log('\n📋 观察结果:', JSON.stringify(obs, null, 2));
      break;

    case 'learn':
      await engine.init();
      const learning = await engine.learn();
      console.log('\n📋 学习结果:', JSON.stringify(learning, null, 2));
      break;

    case 'evolve':
      await engine.init();
      const evoResult = await engine.evolve();
      console.log('\n📋 进化结果:', JSON.stringify(evoResult, null, 2));
      break;

    case 'suggest':
      await engine.init();
      await engine.observe();
      const suggestions = await engine.generateEvolutionSuggestions();
      console.log('\n💡 进化建议:');
      console.log('─'.repeat(40));
      console.log(`   信号数: ${suggestions.signals}`);
      console.log(`   模式数: ${suggestions.patterns}`);
      console.log(`   推荐策略: ${suggestions.strategy}`);
      console.log(`   VFM 权重: ${JSON.stringify(suggestions.vfmWeights)}`);
      console.log(`   PCEC 停滞: ${suggestions.stagnantCycles} 次`);
      if (suggestions.suggestions.length === 0) {
        console.log('   暂无建议，继续观察...');
      } else {
        for (const s of suggestions.suggestions) {
          console.log(`[${s.priority}] ${s.type}: ${s.description || '-'}`);
          if (s.prompt) console.log(`    Prompt: ${s.prompt.slice(0, 80)}...`);
        }
      }
      break;

    case 'status':
      await engine.init();
      const status = engine.getStatus();
      // 从事件日志计算实际观察周期数（events.jsonl 中 observe_complete 事件数）
      const { loadEvents } = require('./src/gep/store');
      const events = loadEvents();
      const observeCount = events.filter(e => e.event_type === 'observe_complete').length;
      console.log('\n📊 系统状态:');
      console.log('─'.repeat(40));
      console.log(`   周期数: ${observeCount} (累计观察次数)`);
      console.log(`   Gene 数: ${status.genes}`);
      console.log(`   Capsule 数: ${status.capsules}`);
      console.log(`   事件数: ${status.events}`);
      console.log(`   能力树节点: ${status.capabilityTree.totalNodes}`);
      console.log(`   人格状态: ${status.personality.mood} (信心: ${(status.personality.confidence * 100).toFixed(0)}%)`);
      console.log('\n   事件统计:');
      for (const [type, count] of Object.entries(status.eventSummary)) {
        console.log(`     ${type}: ${count}`);
      }
      break;

    case 'memory':
      const memoryCmd = args[0];
      await engine.init();
      
      if (memoryCmd === 'status') {
        const memStatus = await engine.getMemoryStatus();
        console.log('\n📚 记忆状态:');
        console.log('─'.repeat(40));
        console.log(`   短时记忆: ${JSON.stringify(memStatus.shortTerm)}`);
        console.log(`   ReMeLight: ${memStatus.reme.description}`);
        console.log(`   监控文件: ${memStatus.reme.monitoredFiles} 个`);
        console.log(`   长时记忆: ${JSON.stringify(memStatus.longTerm)}`);
      } else if (memoryCmd === 'search') {
        console.log('   ℹ️  向量搜索由 CoPaw ReMeLight 在会话中自动提供');
        console.log('   请使用 memory_search 工具或在 CoPaw 会话中查询。');
      } else {
        console.log('用法: node index.js memory status | search <内容>');
      }
      break;

    case 'heartbeat':
      // 启动心跳模式
      await engine.init();
      const { AutoEvoScheduler } = require('./src/core/scheduler');
      const config = await loadConfig();
      const scheduler = new AutoEvoScheduler({
        intervalMs: (config.scheduler?.interval_min || 30) * 60 * 1000,
        minIntervalMs: (config.scheduler?.min_interval_min || 10) * 60 * 1000,
        maxIntervalMs: (config.scheduler?.max_interval_min || 240) * 60 * 1000,
        onCycle: async (n) => {
          console.log(`\n🫀 [心跳 #${n}] ${new Date().toISOString()}`);
          await engine.runCycle();
        },
      });
      scheduler.start();
      console.log('\n💓 XM-AUTO-EVO 进入心跳模式，按 Ctrl+C 停止');
      // 保持进程
      process.on('SIGINT', () => {
        console.log('\n\n👋 停止心跳');
        scheduler.stop();
        process.exit(0);
      });
      break;

    case 'genes':
      const genes = require('./src/gep/store').loadGenes();
      console.log(`\n🧬 已有的 Gene (${genes.length}):`);
      for (const g of genes) {
        console.log(`   [${g.category}] ${g.id}: ${g.signals_match?.join(', ') || '-'}`);
      }
      break;

    case 'help':
      console.log(`
🧬 XM-AUTO-EVO 命令帮助

  start        运行完整进化周期（观察+学习+进化）
  observe      仅执行观察阶段
  learn        仅执行学习阶段
  evolve       仅执行进化阶段
  suggest      生成进化建议
  status       查看系统状态
  memory       查看/搜索记忆
               - status: 查看记忆状态
               - search <内容>: 语义搜索记忆
  heartbeat    启动心跳模式（定时自动循环）
  genes        查看已有 Gene
  help         显示此帮助

示例:
  node index.js start
  node index.js suggest
  node index.js memory search 帮我整理文件
  node index.js heartbeat
      `);
      break;

    default:
      if (!command) {
        console.log('❓ 未知命令。运行 `node index.js help` 查看可用命令。');
      } else {
        console.log(`❓ 未知命令: ${command}`);
        console.log('运行 `node index.js help` 查看可用命令。');
      }
      break;
  }
}

main().catch(e => {
  console.error('❌ 错误:', e.message);
  process.exit(1);
});

     