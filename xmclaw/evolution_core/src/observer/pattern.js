/**
 * 🔍 模式识别器
 * 
 * 功能：从对话历史中检测重复模式
 * 借鉴 Hermes 的自动学习理念
 */

const fs = require('fs').promises;
const path = require('path');

class PatternMatcher {
  constructor(config) {
    this.config = config;
    this.patterns = new Map();
    this.knownPatterns = [];
    this._lastRepeatingPatterns = []; // 缓存上次检测到的重复模式
  }

  /**
   * 获取最近检测到的重复模式（供 engine.js 调用）
   */
  getRepeatingPatterns() {
    return this._lastRepeatingPatterns;
  }

  /**
   * 从对话中提取模式
   * @param {Array<string|{user:string}>} conversations - 字符串数组或 {user} 对象数组
   */
  extractPatterns(conversations) {
    const extracted = [];
    
    for (const conv of conversations) {
      // 支持字符串数组（engine 传 strings）或对象数组（{user}）
      const message = typeof conv === 'string' ? conv : (conv.user || '');
      if (!message) continue;
      
      // 提取意图模式
      const intentPattern = this.extractIntentPattern(message);
      if (intentPattern) extracted.push(intentPattern);
      
      // 提取主题模式
      const topicPattern = this.extractTopicPattern(message);
      if (topicPattern) extracted.push(topicPattern);
      
      // 提取工具使用模式（仅当传入对象时）
      if (typeof conv !== 'string') {
        const toolPattern = this.extractToolPattern(conv);
        if (toolPattern) extracted.push(toolPattern);
      }
    }
    
    return extracted;
  }

  /**
   * 提取意图模式
   */
  extractIntentPattern(message) {
    const lower = message.toLowerCase();
    
    // 搜索类
    if (/搜索|查询|找一下|帮我查/.test(lower)) {
      return {
        type: 'intent',
        category: 'web_search',
        signature: 'search_query',
        example: message.slice(0, 50),
        confidence: 0.9
      };
    }
    
    // 文件操作类
    if (/读取|写入|创建|修改|删除.*文件/.test(lower)) {
      return {
        type: 'intent',
        category: 'file_operation',
        signature: 'file_op',
        example: message.slice(0, 50),
        confidence: 0.9
      };
    }
    
    // 代码生成类
    if (/写.*代码|生成.*代码|帮我写|创建一个.*脚本/.test(lower)) {
      return {
        type: 'intent',
        category: 'code_generation',
        signature: 'code_gen',
        example: message.slice(0, 50),
        confidence: 0.9
      };
    }
    
    // 分析类
    if (/分析|对比|比较|评估/.test(lower)) {
      return {
        type: 'intent',
        category: 'analysis',
        signature: 'analysis',
        example: message.slice(0, 50),
        confidence: 0.8
      };
    }
    
    // 天气查询
    if (/天气/.test(message)) {
      return {
        type: 'intent',
        category: 'weather_query',
        signature: 'weather',
        example: message.slice(0, 50),
        confidence: 0.95
      };
    }
    
    // 新闻/资讯
    if (/新闻|资讯|今天.*发生/.test(lower)) {
      return {
        type: 'intent',
        category: 'news_query',
        signature: 'news',
        example: message.slice(0, 50),
        confidence: 0.85
      };
    }
    
    return null;
  }

  /**
   * 提取主题模式
   */
  extractTopicPattern(message) {
    // 检测实体/关键词
    const entities = this.extractEntities(message);

    // 提高 entity_reference 的门槛，避免每条消息都触发
    const highValueEntities = entities.filter(e =>
      e.type === 'url' || e.type === 'path' || (e.type === 'product' && e.value.length >= 2)
    );

    if (highValueEntities.length >= 2) {
      return {
        type: 'topic',
        category: 'entity_reference',
        entities: highValueEntities,
        example: message.slice(0, 50),
        confidence: 0.6
      };
    }

    return null;
  }

  /**
   * 提取实体（简化的NER）
   */
  extractEntities(message) {
    const entities = [];
    
    // URL
    const urls = message.match(/https?:\/\/[^\s]+/g);
    if (urls) {
      entities.push(...urls.map(u => ({ type: 'url', value: u })));
    }
    
    // 文件路径
    const paths = message.match(/[A-Za-z]:\\[\w\\]+|\/[\w\/]+/g);
    if (paths) {
      entities.push(...paths.map(p => ({ type: 'path', value: p })));
    }
    
    // 产品/服务名（常见模式）
    const products = ['飞书', '钉钉', '微信', 'CoPaw', 'Hermes', 'OpenClaw'];
    for (const product of products) {
      if (message.includes(product)) {
        entities.push({ type: 'product', value: product });
      }
    }
    
    return entities;
  }

  /**
   * 提取工具使用模式
   */
  extractToolPattern(conv) {
    if (!conv.meta || !conv.meta.toolUsed) return null;
    
    return {
      type: 'tool_usage',
      category: conv.meta.toolUsed,
      context: conv.user.slice(0, 30),
      confidence: 0.95
    };
  }

  /**
   * 检测重复模式
   */
  detectRepeatingPatterns(allPatterns) {
    const patternCounts = {};
    
    for (const pattern of allPatterns) {
      const key = pattern.signature || pattern.category;
      
      if (!patternCounts[key]) {
        patternCounts[key] = {
          ...pattern,
          count: 0,
          examples: []
        };
      }
      
      patternCounts[key].count++;
      if (patternCounts[key].examples.length < 3) {
        patternCounts[key].examples.push(pattern.example);
      }
    }
    
    // 返回满足阈值的模式
    const threshold = this.config.pattern_detection?.min_occurrences || 3;
    
    const result = Object.values(patternCounts)
      .filter(p => p.count >= threshold)
      .sort((a, b) => b.count - a.count);
    
    this._lastRepeatingPatterns = result;
    return result;
  }

  /**
   * 检查是否为新模式
   */
  isNovelPattern(pattern, existingGenes) {
    if (!existingGenes || existingGenes.length === 0) {
      return { novel: true, similarity: 0 };
    }
    
    for (const gene of existingGenes) {
      const similarity = this.calculateSimilarity(pattern, gene);
      
      if (similarity > 0.7) {
        return { novel: false, similarity, matchedGene: gene.id };
      }
    }
    
    return { novel: true, similarity: 0 };
  }

  /**
   * 计算模式相似度
   */
  calculateSimilarity(p1, p2) {
    // 简单的关键词重叠度
    const keywords1 = new Set((p1.example || '').match(/\w+/g) || []);
    const keywords2 = new Set((p2.strategy || []).join(' ').match(/\w+/g) || []);
    
    if (keywords1.size === 0 || keywords2.size === 0) return 0;
    
    let intersection = 0;
    for (const word of keywords1) {
      if (keywords2.has(word)) intersection++;
    }
    
    return intersection / Math.sqrt(keywords1.size * keywords2.size);
  }

  /**
   * 生成模式报告
   */
  generateReport(repeatingPatterns, allPatterns) {
    const report = {
      timestamp: Date.now(),
      totalPatterns: allPatterns.length,
      uniquePatterns: new Set(allPatterns.map(p => p.signature || p.category)).size,
      repeatingPatterns: repeatingPatterns.length,
      patterns: repeatingPatterns,
      recommendations: []
    };
    
    // 生成建议
    for (const pattern of repeatingPatterns) {
      if (pattern.count >= 5) {
        report.recommendations.push({
          type: 'high_frequency',
          pattern: pattern.category,
          message: `${pattern.category} 出现 ${pattern.count} 次，建议创建专用 Gene`,
          priority: 'high'
        });
      }
      
      if (pattern.type === 'intent' && pattern.count >= 3) {
        report.recommendations.push({
          type: 'intent_optimization',
          pattern: pattern.category,
          message: `意图 ${pattern.category} 可优化为快捷技能`,
          priority: 'medium'
        });
      }
    }
    
    return report;
  }
}

module.exports = PatternMatcher;
