#!/usr/bin/env node
'use strict';

// 测试修复后的去重逻辑

const fs = require('node:fs');
const path = require('path');

// 加载 CrossSessionAnalyzer
const { CrossSessionAnalyzer } = require('../src/observer/cross_session');

async function testDedupLogic() {
  const analyzer = new CrossSessionAnalyzer(process.cwd());
  const result = analyzer.analyze(50);
  
  console.log('=== 跨会话模式分析 ===\n');
  
  // 只看 repair 和 innovate 模式
  const interestingPatterns = result.patterns.filter(p => 
    p.cross_session && (p.category === 'repair' || p.category === 'innovate')
  );
  
  console.log('检测到的跨会话模式:');
  interestingPatterns.forEach((p, i) => {
    const comboKey = `${p.signature}:${p.session_count}`;
    console.log(`  ${i+1}. [${p.category}] ${p.signature}`);
    console.log(`     session_count: ${p.session_count}`);
    console.log(`     combination_key: ${comboKey}`);
    console.log('');
  });
  
  // 加载 skill.json
  const skillJsonPath = path.join(process.cwd(), 'skill.json');
  const skillJson = JSON.parse(fs.readFileSync(skillJsonPath, 'utf-8'));
  const autoSkills = Object.entries(skillJson.skills || {})
    .filter(([k]) => k.startsWith('auto_'));
  
  console.log('=== skill.json 中的 auto_* 技能 ===\n');
  
  // 提取已有的组合键
  const existingCombinations = new Set();
  autoSkills.forEach(([skillId, skillData]) => {
    const sig = skillData.metadata?.signature || '';
    const desc = skillData.metadata?.description || '';
    const match = desc.match(/影响 (\d+) 个会话/);
    const cnt = match ? match[1] : null;
    
    if (sig) {
      const comboKey = cnt ? `${sig}:${cnt}` : sig;
      existingCombinations.add(comboKey);
      console.log(`  - ${skillId}`);
      console.log(`    signature: "${sig}"`);
      console.log(`    session_count: ${cnt || 'N/A'}`);
      console.log(`    combo_key: ${comboKey}`);
      console.log('');
    }
  });
  
  console.log('=== 去重结果 ===\n');
  
  // 模拟去重逻辑
  interestingPatterns.forEach((p, i) => {
    const comboKey = `${p.signature}:${p.session_count}`;
    const sigOnlyKey = p.signature;
    
    const comboExists = existingCombinations.has(comboKey);
    const sigOnlyExists = existingCombinations.has(sigOnlyKey);
    
    const willCreate = !comboExists && !sigOnlyExists;
    
    console.log(`  ${i+1}. [${p.category}] ${p.signature}:${p.session_count}`);
    console.log(`     combo_key 存在: ${comboExists}`);
    console.log(`     sig_only 存在: ${sigOnlyExists}`);
    console.log(`     ❌ 跳过 - 已存在` : `     ✅ 将创建新 Skill`);
    console.log('');
  });
}

testDedupLogic().catch(console.error);
