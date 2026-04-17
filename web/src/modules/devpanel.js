/** @module devpanel - Dev view: Plan, File Changes, Diff, Docs */
import { showToast } from './notifications.js';

// ── State ─────────────────────────────────────────────────────────────────────
let _open = false;
let _activeTab = 'plan';      // 'plan' | 'files' | 'diff'
let _fileChanges = [];         // [{path, action, oldContent, newContent}]
let _currentDiffFile = null;
let _planSteps = [];          // [{text, done, active}]
let _planVersion = 0;         // increment to re-render plan

// ── DOM refs ──────────────────────────────────────────────────────────────────
let _overlay = null;
let _panel = null;
let _tabBtns = null;
let _body = null;

// ── Init ───────────────────────────────────────────────────────────────────────
function _ensureDOM() {
    if (_panel) return;
    _overlay = document.getElementById('dev-overlay');
    _panel = document.getElementById('dev-panel');
    _tabBtns = document.querySelectorAll('.dev-tab');
    _body = document.getElementById('dev-panel-body');
}

// ── Public API ────────────────────────────────────────────────────────────────
function openDevPanel() {
    _ensureDOM();
    _open = true;
    _overlay?.classList.add('open');
    _panel?.classList.add('open');
    render();
}

function closeDevPanel() {
    _open = false;
    _overlay?.classList.remove('open');
    _panel?.classList.remove('open');
}

function toggleDevPanel() {
    _open ? closeDevPanel() : openDevPanel();
}

function switchDevTab(tab) {
    _activeTab = tab;
    _tabBtns?.forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
    render();
}

/** Add a file operation event. Triggers dev panel open on first file op. */
function addFileChange(path, action, oldContent, newContent) {
    oldContent = oldContent || '';
    newContent = newContent || '';
    const existing = _fileChanges.findIndex(f => f.path === path);
    if (existing >= 0) {
        _fileChanges[existing] = { path, action,
            oldContent: _fileChanges[existing].oldContent || oldContent,
            newContent };
    } else {
        _fileChanges.push({ path, action, oldContent, newContent });
    }
    if (!_open && _fileChanges.length > 0) {
        _activeTab = 'files';
        openDevPanel();
    } else {
        render();
    }
}

/** Update plan steps from a plan text. Parses numbered list into steps. */
function setPlan(planText) {
    _planSteps = _parsePlan(planText || '');
    _planVersion++;
    render();
}

/** Mark a plan step as done by index. */
function markPlanStepDone(idx) {
    if (_planSteps[idx]) {
        _planSteps[idx].done = true;
        _planSteps[idx].active = false;
        if (_planSteps[idx + 1]) _planSteps[idx + 1].active = true;
        render();
    }
}

/** Parse plain text plan into structured steps. */
function _parsePlan(text) {
    if (!text) return [];
    const lines = text.split('\n');
    const steps = [];
    for (const line of lines) {
        const trimmed = line.trim();
        // Match numbered items: "1. Do something", "1) Do something"
        const m = trimmed.match(/^(\d+)[.)：、\s]+(.+)/);
        if (m) {
            steps.push({ num: parseInt(m[1], 10), text: m[2].trim(), done: false, active: steps.length === 0 });
        } else if (trimmed.startsWith('- ') || trimmed.startsWith('* ')) {
            const bullet = trimmed.slice(2).trim();
            if (steps.length > 0) {
                steps[steps.length - 1].text += ' · ' + bullet;
            } else {
                steps.push({ num: 1, text: bullet, done: false, active: true });
            }
        } else if (trimmed.length > 3 && !trimmed.startsWith('#')) {
            if (steps.length === 0) {
                steps.push({ num: 1, text: trimmed, done: false, active: true });
            }
        }
    }
    // Fallback: split by sentence separators
    if (steps.length === 0 && text.trim()) {
        const sentences = text.split(/[.。;；]/).filter(s => s.trim().length > 4);
        sentences.slice(0, 10).forEach((s, i) => {
            steps.push({ num: i + 1, text: s.trim(), done: false, active: i === 0 });
        });
    }
    return steps;
}

/** Clear all dev panel state. */
function clearDevPanel() {
    _fileChanges = [];
    _currentDiffFile = null;
    _planSteps = [];
    _planVersion++;
    render();
}

// ── Render ────────────────────────────────────────────────────────────────────
function render() {
    _ensureDOM();
    if (!_body) return;
    _body.innerHTML = '';

    if (_activeTab === 'plan') {
        _body.appendChild(_renderPlan());
    } else if (_activeTab === 'files') {
        _body.appendChild(_renderFiles());
    } else if (_activeTab === 'diff') {
        _body.appendChild(_renderDiff());
    }
}

// ── Tab: Plan ─────────────────────────────────────────────────────────────────
function _renderPlan() {
    const wrap = document.createElement('div');

    if (_planSteps.length === 0) {
        wrap.innerHTML = '<div class="empty-state" style="margin-top:20px">暂无执行计划<br><br><span style="font-size:11px;color:var(--text-faint)">开启计划模式后，Agent 的执行计划将显示在此处</span></div>';
        return wrap;
    }

    const done = _planSteps.filter(s => s.done).length;
    const total = _planSteps.length;
    const progress = total > 0 ? Math.round((done / total) * 100) : 0;

    const header = document.createElement('div');
    header.style.cssText = 'display:flex;align-items:center;gap:10px;margin-bottom:12px';
    header.innerHTML =
        '<div style="flex:1">' +
            '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px">' +
                '<span style="font-size:12px;font-weight:600">执行计划</span>' +
                '<span style="font-size:11px;color:var(--text-dim)">' + done + '/' + total + ' 完成</span>' +
            '</div>' +
            '<div style="height:4px;background:var(--surface-2);border-radius:2px;overflow:hidden">' +
                '<div style="height:100%;width:' + progress + '%;background:var(--accent);border-radius:2px;transition:width 0.3s"></div>' +
            '</div>' +
        '</div>';
    wrap.appendChild(header);

    const stepsEl = document.createElement('div');
    stepsEl.className = 'plan-steps';
    _planSteps.forEach((step, i) => {
        const stepEl = document.createElement('div');
        stepEl.className = 'plan-step' + (step.done ? ' done' : '') + (step.active ? ' active' : '');
        const icon = step.done
            ? '<svg width="8" height="8" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="2 6 5 9 10 3"/></svg>'
            : (step.active ? '<svg width="8" height="8" viewBox="0 0 12 12"><circle cx="6" cy="6" r="4" fill="currentColor"/></svg>'
            : '<span>' + step.num + '</span>');
        stepEl.innerHTML = '<div class="plan-step-icon">' + icon + '</div><div class="plan-step-text">' + _esc(step.text) + '</div>';
        if (!step.done) {
            stepEl.style.cursor = 'pointer';
            stepEl.title = '点击标记完成';
            stepEl.addEventListener('click', () => markPlanStepDone(i));
        }
        stepsEl.appendChild(stepEl);
    });
    wrap.appendChild(stepsEl);

    const clearBtn = document.createElement('button');
    clearBtn.className = 'sc-btn red';
    clearBtn.style.cssText = 'margin-top:8px;align-self:flex-start';
    clearBtn.textContent = '清空计划';
    clearBtn.addEventListener('click', () => { _planSteps = []; _planVersion++; render(); });
    wrap.appendChild(clearBtn);

    return wrap;
}

// ── Tab: Files ─────────────────────────────────────────────────────────────────
function _renderFiles() {
    const wrap = document.createElement('div');

    if (_fileChanges.length === 0) {
        wrap.innerHTML = '<div class="empty-state" style="margin-top:20px">暂无文件变更<br><span style="font-size:11px;color:var(--text-faint)">Agent 修改文件时会自动记录</span></div>';
        return wrap;
    }

    const header = document.createElement('div');
    header.className = 'file-changes-header';
    header.innerHTML = '<span style="font-size:12px;font-weight:600">文件变更</span><span class="file-change-count">' + _fileChanges.length + ' 个文件</span>';
    wrap.appendChild(header);

    _fileChanges.forEach((fc) => {
        const icon = fc.action === 'write' ? '&#128396;' : fc.action === 'read' ? '&#128065;' : '&#128465;';
        const actionLabel = fc.action === 'write' ? '写入' : fc.action === 'read' ? '读取' : '删除';
        const el = document.createElement('div');
        el.className = 'file-change-item';
        el.innerHTML = '<span class="fc-icon">' + icon + '</span><span class="fc-name">' + _esc(fc.path) + '</span><span class="fc-action ' + fc.action + '">' + actionLabel + '</span>';
        el.addEventListener('click', () => {
            _currentDiffFile = fc;
            _activeTab = 'diff';
            _tabBtns?.forEach(b => b.classList.toggle('active', b.dataset.tab === 'diff'));
            render();
        });
        wrap.appendChild(el);
    });

    const clearBtn = document.createElement('button');
    clearBtn.className = 'sc-btn red';
    clearBtn.style.cssText = 'margin-top:8px;align-self:flex-start';
    clearBtn.textContent = '清空记录';
    clearBtn.addEventListener('click', () => { _fileChanges = []; _currentDiffFile = null; _planVersion++; render(); });
    wrap.appendChild(clearBtn);

    return wrap;
}

// ── Tab: Diff ─────────────────────────────────────────────────────────────────
function _renderDiff() {
    const wrap = document.createElement('div');

    if (!_currentDiffFile) {
        if (_fileChanges.length === 0) {
            wrap.innerHTML = '<div class="empty-state" style="margin-top:20px">暂无代码变更</div>';
            return wrap;
        }
        const header = document.createElement('div');
        header.style.cssText = 'font-size:12px;font-weight:600;margin-bottom:10px';
        header.textContent = '选择文件查看变更';
        wrap.appendChild(header);
        _fileChanges.forEach((fc) => {
            const el = document.createElement('div');
            el.className = 'file-change-item';
            el.innerHTML = '<span class="fc-icon">&#128196;</span><span class="fc-name">' + _esc(fc.path) + '</span>';
            el.addEventListener('click', () => { _currentDiffFile = fc; render(); });
            wrap.appendChild(el);
        });
        return wrap;
    }

    const fc = _currentDiffFile;
    const oldLines = (fc.oldContent || '').split('\n');
    const newLines = (fc.newContent || '').split('\n');
    const diff = _computeDiff(oldLines, newLines);
    const addCount = diff.filter(d => d.type === 'add').length;
    const delCount = diff.filter(d => d.type === 'del').length;

    const fileHeader = document.createElement('div');
    fileHeader.className = 'diff-file-header';
    fileHeader.innerHTML = '<span class="diff-file-name">' + _esc(fc.path) + '</span>' +
        '<span class="diff-stats"><span class="diff-add">+' + addCount + '</span>&nbsp;<span class="diff-del">-' + delCount + '</span></span>';
    wrap.appendChild(fileHeader);

    const container = document.createElement('div');
    container.className = 'diff-container';

    if (diff.length === 0) {
        const emptyEl = document.createElement('div');
        emptyEl.className = 'diff-line';
        emptyEl.style.cssText = 'color:var(--text-dim);font-size:11px;padding:10px';
        emptyEl.textContent = '无法计算差异，内容如下：';
        container.appendChild(emptyEl);
        const content = fc.newContent || fc.oldContent || '';
        content.split('\n').forEach((line, i) => {
            const el = document.createElement('div');
            el.className = 'diff-line';
            el.innerHTML = '<span class="diff-line-num">' + (i + 1) + '</span><span class="diff-line-sign">&nbsp;</span><span class="diff-line-content">' + _esc(line) + '</span>';
            container.appendChild(el);
        });
    } else {
        diff.forEach((d) => {
            const el = document.createElement('div');
            el.className = 'diff-line' + (d.type === 'add' ? ' add' : d.type === 'del' ? ' del' : '');
            const sign = d.type === 'add' ? '+' : d.type === 'del' ? '-' : ' ';
            el.innerHTML = '<span class="diff-line-num">' + (d.line || '') + '</span><span class="diff-line-sign">' + sign + '</span><span class="diff-line-content">' + _esc(d.content) + '</span>';
            container.appendChild(el);
        });
    }
    wrap.appendChild(container);

    const back = document.createElement('button');
    back.className = 'sc-btn';
    back.style.cssText = 'margin-top:8px;align-self:flex-start';
    back.textContent = '\u2190 \u8fd4\u56de\u6587\u4ef6\u5217\u8868';
    back.addEventListener('click', () => { _currentDiffFile = null; render(); });
    wrap.appendChild(back);

    return wrap;
}

/** Simple line-level diff: additions vs deletions. */
function _computeDiff(oldLines, newLines) {
    const result = [];
    const oldSet = new Set(oldLines);
    const newSet = new Set(newLines);
    newLines.forEach((line, i) => {
        if (!oldSet.has(line) || oldLines[i] !== line) {
            result.push({ type: 'add', content: line, line: i + 1 });
        }
    });
    oldLines.forEach((line, i) => {
        if (!newSet.has(line)) {
            result.push({ type: 'del', content: line, line: i + 1 });
        }
    });
    result.sort((a, b) => (a.line || 0) - (b.line || 0));
    return result;
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function _esc(str) {
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

// ── Wire to global window ─────────────────────────────────────────────────────
window._devPanel = {
    open: openDevPanel,
    close: closeDevPanel,
    toggle: toggleDevPanel,
    switchTab: switchDevTab,
    addFileChange,
    setPlan,
    markPlanStep: markPlanStepDone,
    clear: clearDevPanel,
};

export { openDevPanel, closeDevPanel, toggleDevPanel, switchDevTab,
         addFileChange, setPlan, markPlanStepDone, clearDevPanel };
