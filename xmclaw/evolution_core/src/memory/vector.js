'use strict';

/**
 * 向量记忆适配器 (CoPaw ReMeLight 版)
 *
 * xm-auto-evo 不再维护独立的向量后端，
 * 完全依赖 CoPaw 自带的 ReMeLight 向量记忆系统。
 *
 * ReMeLight 能力：
 * - FileWatcher 自动监控 .md 文件变更
 * - 向量 + 全文检索 via memory_search 工具
 * - 自动索引 MEMORY.md 和 memory/*.md
 */

// ── 初始化 ──────────────────────────────────────
function initFromConfig(config) {
  if (config && (config.reme || config.vector)) {
    console.log('   📦 向量记忆: CoPaw ReMeLight 集成模式');
  }
}

module.exports = {
  initFromConfig,
};
