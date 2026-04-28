/**
 * 🧠 长时记忆模块
 * 
 * 功能：管理 MEMORY.md 和知识库
 * 生命周期：永久
 * 自动更新：根据中时记忆的摘要
 */

const fs = require('fs').promises;
const path = require('path');

class LongTermMemory {
  constructor(workspace) {
    this.workspace = workspace;
    // 使用 CoPaw 原生 MEMORY.md（根目录）
    this.memoryFile = path.join(workspace, 'MEMORY.md');
    this.backupDir = path.join(workspace, 'memory', 'backups');
  }

  async init() {
    try {
      await fs.mkdir(this.backupDir, { recursive: true });
    } catch (e) {
      // ignore
    }
    
    // 确保 MEMORY.md 存在
    try {
      await fs.access(this.memoryFile);
    } catch (e) {
      await this.createDefaultMemory();
    }
  }

  /**
   * 创建默认的 MEMORY.md
   */
  async createDefaultMemory() {
    const content = `# 🧠 长期记忆

> 最后更新: ${new Date().toISOString().split('T')[0]}

---

## 👤 用户信息

| 项目 | 内容 |
|------|------|
| 名字 | |
| 称呼 | |
| 主要语言 | 中文 |
| 偏好 | |

---

## 🔧 工具设置

### 已配置的工具

（由 xm-auto-evo 自动维护）

---

## 💡 经验教训

### 自动学习总结

（由 xm-auto-evo 根据中时记忆自动更新）

---

*记忆由小悦自动维护，最后更新于 ${new Date().toISOString().split('T')[0]}*
`;
    
    await fs.writeFile(this.memoryFile, content);
  }

  /**
   * 读取当前记忆
   */
  async read() {
    try {
      return await fs.readFile(this.memoryFile, 'utf-8');
    } catch (e) {
      return null;
    }
  }

  /**
   * 更新用户信息
   */
  async updateUserInfo(info) {
    const content = await this.read();
    
    // 简单的文本替换
    let newContent = content;
    
    if (info.name) {
      newContent = this.updateTableValue(newContent, '名字', info.name);
    }
    if (info.称呼) {
      newContent = this.updateTableValue(newContent, '称呼', info.称呼);
    }
    if (info.preference) {
      newContent = this.updateTableValue(newContent, '偏好', info.preference);
    }
    
    newContent = this.updateTimestamp(newContent);
    
    await this.backup();
    await fs.writeFile(this.memoryFile, newContent);
    
    return newContent;
  }

  /**
   * 更新表格中的值
   */
  updateTableValue(content, key, value) {
    const lines = content.split('\n');
    let inTable = false;
    let keyFound = false;
    
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      
      // 检测表格
      if (line.includes('|') && line.includes(key)) {
        inTable = true;
      }
      
      // 在表格中查找并更新值
      if (inTable && line.includes(`| ${key}`)) {
        const parts = line.split('|');
        if (parts.length >= 3) {
          parts[parts.length - 2] = ` ${value} `;
          lines[i] = parts.join('|');
          keyFound = true;
          break;
        }
      }
    }
    
    return lines.join('\n');
  }

  /**
   * 更新经验教训（自动去重）
   */
  async addLesson(category, lesson) {
    const content = await this.read();
    
    // 去重：检查是否已存在完全相同的 lesson
    if (content.includes(lesson)) {
      return content; // 已存在，跳过
    }
    
    const lessonEntry = `\n- **${new Date().toISOString().split('T')[0]}** ${lesson}`;
    
    // 查找或创建经验教训部分
    let newContent;
    const lessonsSection = '### 自动学习总结';
    
    if (content.includes(lessonsSection)) {
      newContent = content.replace(
        lessonsSection,
        `${lessonsSection}${lessonEntry}`
      );
    } else {
      const insertPoint = '## 💡 经验教训';
      if (content.includes(insertPoint)) {
        newContent = content.replace(
          insertPoint,
          `${insertPoint}\n\n### 自动学习总结${lessonEntry}`
        );
      } else {
        // 在文件末尾添加
        newContent = content + `\n\n## 💡 经验教训\n\n### 自动学习总结${lessonEntry}`;
      }
    }
    
    newContent = this.updateTimestamp(newContent);
    
    await this.backup();
    await fs.writeFile(this.memoryFile, newContent);
    
    return newContent;
  }

  /**
   * 添加洞察（addLesson 的别名，供 engine.js 调用）
   */
  addInsight(insight) {
    const message = typeof insight === 'string' ? insight : (insight.message || insight.description || JSON.stringify(insight));
    return this.addLesson('insight', message);
  }

  /**
   * 添加新能力记录
   */
  async addCapability(capability) {
    const content = await this.read();
    
    const capabilityEntry = `\n| ${capability.name} | ${capability.description} | ${new Date().toISOString().split('T')[0]} |`;
    
    // 查找或创建能力表格
    let newContent;
    const capabilitySection = '### 已配置的工具';
    
    if (content.includes(capabilitySection)) {
      // 检查是否已有表格
      if (content.includes('| 名称 |')) {
        newContent = content.replace(
          /(### 已配置的工具\n[\s\S]*?)(\n---)/,
          `$1${capabilityEntry}\n`
        );
      } else {
        newContent = content.replace(
          capabilitySection,
          `${capabilitySection}\n\n| 名称 | 描述 | 添加时间 |\n|------|------|----------|${capabilityEntry}`
        );
      }
    }
    
    if (newContent) {
      newContent = this.updateTimestamp(newContent);
      await this.backup();
      await fs.writeFile(this.memoryFile, newContent);
    }
    
    return newContent;
  }

  /**
   * 更新时间戳
   */
  updateTimestamp(content) {
    const date = new Date().toISOString().split('T')[0];
    return content.replace(/最后更新: \d{4}-\d{2}-\d{2}/, `最后更新: ${date}`);
  }

  /**
   * 备份当前版本
   */
  async backup() {
    try {
      const content = await this.read();
      const timestamp = Date.now();
      const backupFile = path.join(this.backupDir, `MEMORY_${timestamp}.md`);
      await fs.writeFile(backupFile, content);
      
      // 只保留最近10个备份
      const files = await fs.readdir(this.backupDir);
      if (files.length > 10) {
        const sorted = files.sort().slice(0, files.length - 10);
        for (const file of sorted) {
          await fs.unlink(path.join(this.backupDir, file));
        }
      }
    } catch (e) {
      // ignore
    }
  }

  /**
   * 根据中时记忆摘要更新
   */
  async syncFromMediumTerm(summary) {
    if (!summary || !summary.insights) return;
    
    for (const insight of summary.insights) {
      if (insight.priority === 'high') {
        await this.addLesson('trend', insight.message);
      }
    }
    
    // 更新顶部的时间戳
    const content = await this.read();
    const newContent = this.updateTimestamp(content);
    if (newContent !== content) {
      await fs.writeFile(this.memoryFile, newContent);
    }
  }

  /**
   * 获取记忆统计
   */
  async getStats() {
    const content = await this.read();
    
    return {
      size: content.length,
      lines: content.split('\n').length,
      lastModified: (await fs.stat(this.memoryFile)).mtime,
      sections: this.countSections(content)
    };
  }

  /**
   * 统计章节数
   */
  countSections(content) {
    const matches = content.match(/^##\s+/gm);
    return matches ? matches.length : 0;
  }
}

module.exports = LongTermMemory;
