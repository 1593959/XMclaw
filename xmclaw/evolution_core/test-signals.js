// 测试新的 signals.js - CoPaw 格式
const { extractSignals, getSignalStats } = require('./src/observer/signals');

const dialogDir = 'E:/新建文件夹/CoPaw/workspaces/default/dialog';
const sessionsDir = 'E:/新建文件夹/CoPaw/workspaces/default/sessions';

console.log('=== CoPaw 信号提取测试 ===\n');

// 获取统计摘要
const stats = getSignalStats(dialogDir, 7);
console.log('📊 信号统计:');
console.log('  总条目:', stats.totalEntries);
console.log('  总信号类型:', stats.totalSignals);
console.log('\n  Top 工具调用:');
for (const [t, c] of stats.topTools) console.log('    ' + t + ': ' + c + '次');
console.log('\n  Top 意图:');
for (const [i, c] of stats.topIntents) console.log('    ' + i + ': ' + c + '次');
console.log('\n  Top 信号:');
for (const [s, c] of stats.topSignals) console.log('    ' + s + ': ' + c + '次');

// 提取信号
const signals = extractSignals(dialogDir, 7, 500);
console.log('\n提取到 ' + signals.length + ' 个信号');
console.log('前20个:', signals.slice(0, 20));

if (signals.length > 0) {
  console.log('\n✅ 信号提取成功！');
} else {
  console.log('\n⚠️ 无信号，需检查数据');
}
