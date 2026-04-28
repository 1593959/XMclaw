// 检查 auto_* 技能的 signature
const fs = require('node:fs');
const skillJson = JSON.parse(fs.readFileSync('./skill.json', 'utf-8'));

console.log('=== auto_* 技能 signature 检查 ===\n');

Object.entries(skillJson.skills || {})
  .filter(([k]) => k.startsWith('auto_'))
  .forEach(([skillId, skillData]) => {
    const sig = skillData.metadata?.signature;
    const desc = skillData.metadata?.description || '';
    const match = desc.match(/影响 (\d+) 个会话/);
    
    console.log(`${skillId}:`);
    console.log(`  signature: "${sig || '(空)'}"`);
    console.log(`  session_count: ${match ? match[1] : 'N/A'}`);
    console.log('');
  });
