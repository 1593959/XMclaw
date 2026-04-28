'use strict';

/**
 * CoPaw 主动通知模块
 *
 * 当 xm-auto-evo 产生重要进化结果时，
 * 通过 copaw channels send 主动推送消息给用户。
 */

const { execSync } = require('child_process');
const path = require('path');

class CopawNotifier {
  constructor(workspace) {
    this.workspace = workspace;
    this.agentId = process.env.COPAW_AGENT_ID || 'default';
    this.channel = process.env.COPAW_CHANNEL || 'console';
    this.targetUser = process.env.COPAW_TARGET_USER || 'default';
    this.targetSession = process.env.COPAW_TARGET_SESSION || 'default';
    this.enabled = !!(this.agentId && this.channel && this.targetUser && this.targetSession);
  }

  /**
   * 发送进化完成通知
   */
  notifyEvolution(result) {
    if (!this.enabled) return false;

    const items = result.items || [];
    if (items.length === 0) return false;

    const geneCount = items.filter(i => i.type === 'gene').length;
    const skillCount = items.filter(i => i.type === 'skill').length;

    const lines = [
      '🧬 xm-auto-evo 进化完成',
      `Gene: +${geneCount} | Skill: +${skillCount}`,
    ];

    for (const item of items) {
      lines.push(`- ${item.type}: ${item.id}`);
    }

    const text = lines.join('\n');
    return this._send(text);
  }

  /**
   * 发送 PCEC 停滞突破通知
   */
  notifyStagnationBreak() {
    if (!this.enabled) return false;
    return this._send('🧬 xm-auto-evo: PCEC 停滞已突破，新周期开始');
  }

  /**
   * 发送 ADL 劣化警告
   */
  notifyDegradation(details) {
    if (!this.enabled) return false;
    return this._send(`⚠️ xm-auto-evo ADL 劣化: ${details}`);
  }

  _send(text) {
    try {
      // Windows cmd 下含换行符的参数传递困难，先把 text 写入临时文件，再用 PowerShell 读取
      const fs = require('fs');
      const tmpFile = path.join(this.workspace, '.evo_notify_tmp.txt');
      fs.writeFileSync(tmpFile, text, 'utf-8');

      const psCmd = `copaw channels send --agent-id ${this.agentId} --channel ${this.channel} --target-user ${this.targetUser} --target-session ${this.targetSession} --text (Get-Content -Raw '${tmpFile.replace(/'/g, "''")}')`;
      execSync(`powershell -NoProfile -Command "${psCmd.replace(/"/g, '\\"')}"`, { cwd: this.workspace, encoding: 'utf-8', timeout: 30000 });
      try { fs.unlinkSync(tmpFile); } catch {}
      return true;
    } catch (e) {
      console.log(`   ⚠️  通知发送失败: ${e.message}`);
      return false;
    }
  }
}

module.exports = { CopawNotifier };
