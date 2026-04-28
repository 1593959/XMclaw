/**
 * Memory Bridge - xm-auto-evo 与 memory skill 的桥梁
 * 
 * 将进化过程中学到的东西自动写入 memory/memories.md 和 memory/user_profile.md
 * 
 * 使用时机：
 * - Gene 固化成功时
 * - Skill 创建成功时
 * - 检测到用户偏好时
 * - 经验教训发现时
 * - 工具配置变更时
 */

const fs = require('fs').promises;
const fsSync = require('fs');
const path = require('path');

// B-18 unification: when running under XMclaw, env XMC_MEMORY_PATH
// points at the canonical persona MEMORY.md (so xm-auto-evo writes
// land in the SAME file the agent's `remember`/`learn_about_user`
// tools update, AND the XMclaw system-prompt assembler reads). Falls
// back to <workspace>/MEMORY.md for standalone CoPaw mode.
function _resolveMemoryFile(workspace) {
    const envPath = process.env.XMC_MEMORY_PATH;
    if (envPath) {
        // ensure the parent directory exists
        try { fsSync.mkdirSync(path.dirname(envPath), { recursive: true }); } catch {}
        return envPath;
    }
    return _resolveMemoryFile(workspace);
}

function _resolveProfileFile(workspace) {
    const envPath = process.env.XMC_PROFILE_PATH;
    if (envPath) {
        try { fsSync.mkdirSync(path.dirname(envPath), { recursive: true }); } catch {}
        return envPath;
    }
    return path.join(workspace || process.cwd(), 'PROFILE.md');
}

// 初始化
async function init(workspace) {
    const memFile = _resolveMemoryFile(workspace);
    const userFile = _resolveProfileFile(workspace);
    
    for (const file of [memFile, userFile]) {
        try { await fs.access(file); } 
        catch { await fs.writeFile(file, `# 记忆\n\n`); }
    }

    return { memFile, userFile };
}

// 解析条目
function parseLine(line) {
    const m = line.trim().match(/^(.+?)\s*\[(\w+)\]\s*$/);
    return m ? { content: m[1], tag: m[2] } : { content: line.trim(), tag: null };
}

// 读取
async function read(file) {
    try {
        const content = await fs.readFile(file, 'utf-8');
        return content.split('\n').map(l => l.trim()).filter(l => l && !l.startsWith('#')).map(parseLine);
    } catch { return []; }
}

// 写入
async function write(file, entries, title) {
    const lines = [`# ${title}`, ''];
    for (const e of entries) {
        const tag = e.tag ? ` [${e.tag}]` : '';
        lines.push(e.content + tag);
    }
    await fs.writeFile(file, lines.join('\n') + '\n');
}

// 记录 Gene 固化成功
async function recordGeneCreated(gene, workspace) {
    await init(workspace);
    const file = _resolveMemoryFile(workspace);
    const entries = await read(file);

    const content = `Gene 固化: ${gene.id} (VFM: ${gene.vfmScore?.toFixed(2) || '?'})`;
    if (entries.some(e => e.content.includes(gene.id))) return; // 避免重复

    entries.push({ content, tag: 'gene' });
    await write(file, entries, 'Agent 记忆');
    console.log(`   📝 Gene 记录: ${gene.id}`);
}

// 记录 Skill 创建成功
async function recordSkillCreated(skill, workspace) {
    await init(workspace);
    const file = _resolveMemoryFile(workspace);
    const entries = await read(file);

    const content = `Skill 创建: ${skill.id || skill.skillId}`;
    if (entries.some(e => e.content.includes(skill.id || skill.skillId))) return;

    entries.push({ content, tag: 'skill' });
    await write(file, entries, 'Agent 记忆');
    console.log(`   📝 Skill 记录: ${skill.id || skill.skillId}`);
}

// 记录用户偏好
async function recordUserPreference(preference, workspace) {
    await init(workspace);
    const file = _resolveProfileFile(workspace);
    const entries = await read(file);

    const content = `${preference}`;
    if (entries.some(e => e.content === content)) return;

    entries.push({ content, tag: 'preference' });
    await write(file, entries, '用户信息');
    console.log(`   📝 偏好记录: ${content.slice(0, 30)}...`);
}

// 记录经验教训
async function recordLesson(lesson, workspace) {
    await init(workspace);
    const file = _resolveMemoryFile(workspace);
    const entries = await read(file);

    const content = `教训: ${lesson}`;
    if (entries.some(e => e.content === content)) return;

    entries.push({ content, tag: 'lesson' });
    await write(file, entries, 'Agent 记忆');
    console.log(`   📝 教训记录: ${lesson.slice(0, 30)}...`);
}

// 记录工具配置
async function recordToolConfig(config, workspace) {
    await init(workspace);
    const file = _resolveMemoryFile(workspace);
    const entries = await read(file);

    const content = `工具配置: ${config}`;
    if (entries.some(e => e.content === content)) return;

    entries.push({ content, tag: 'tool' });
    await write(file, entries, 'Agent 记忆');
    console.log(`   📝 配置记录: ${config}`);
}

// 记录模式发现
async function recordPattern(pattern, workspace) {
    await init(workspace);
    const file = _resolveMemoryFile(workspace);
    const entries = await read(file);

    const content = `模式: ${pattern.description || pattern}`;
    if (entries.some(e => e.content === content)) return;

    entries.push({ content, tag: 'pattern' });
    await write(file, entries, 'Agent 记忆');
    console.log(`   📝 模式记录: ${content.slice(0, 30)}...`);
}

// 批量记录
async function recordBatch(items, workspace) {
    for (const item of items) {
        switch (item.type) {
            case 'gene': await recordGeneCreated(item.data, workspace); break;
            case 'skill': await recordSkillCreated(item.data, workspace); break;
            case 'preference': await recordUserPreference(item.data, workspace); break;
            case 'lesson': await recordLesson(item.data, workspace); break;
            case 'tool': await recordToolConfig(item.data, workspace); break;
            case 'pattern': await recordPattern(item.data, workspace); break;
        }
    }
}

module.exports = {
    init,
    recordGeneCreated,
    recordSkillCreated,
    recordUserPreference,
    recordLesson,
    recordToolConfig,
    recordPattern,
    recordBatch,
};
