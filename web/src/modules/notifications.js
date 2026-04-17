/** @module notifications - Toast and in-app notification rendering */
const _toasts = [];
const MAX_TOASTS = 5;

/** Show a dismissable toast notification. */
function showToast(msg) {
    const el = document.createElement('div');
    el.className = 'toast';
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 2200);
}

/** Render a dismissable toast into a specific container (fallback). */
function showToastIn(el, msg, duration = 4000) {
    const div = document.createElement('div');
    div.className = 'toast-notify';
    div.innerHTML = `<span class="toast-icon">✨</span><span>${escapeHtml(msg)}</span><button class="toast-close">×</button>`;
    div.querySelector('.toast-close').onclick = () => div.remove();
    el.appendChild(div);
    setTimeout(() => div.remove(), duration);
}

/**
 * Handle an `evolution:notify` EventBus event forwarded from the daemon.
 * Shows an in-dashboard notification and updates the evolution timeline.
 * @param {object} payload - event.payload from the EventBus event
 */
function handleEvolutionNotify(payload) {
    const { summary, gene_count = 0, skill_count = 0, actions = [] } = payload;

    // Show a dismissable evolution notification strip at the top
    const container = document.getElementById('evo-notify-container');
    if (container) {
        const icons = [];
        if (gene_count > 0) icons.push(`<span style="color:#a78bfa">✨ ${gene_count} Gene${gene_count > 1 ? 's' : ''}</span>`);
        if (skill_count > 0) icons.push(`<span style="color:#34d399">🛠 ${skill_count} Skill${skill_count > 1 ? 's' : ''}</span>`);
        const iconStr = icons.length ? icons.join(' · ') : '<span style="color:#fbbf24">🔄</span>';

        const div = document.createElement('div');
        div.className = 'evo-notify-strip';
        div.innerHTML = `
            <span>${iconStr}</span>
            <span style="color:#9ca3af;font-size:12px">${escapeHtml(summary)}</span>
            <button class="toast-close" onclick="this.parentElement.remove()" style="margin-left:auto">×</button>
        `;
        container.appendChild(div);
        setTimeout(() => div.remove(), 10000);
    }

    // Update gene/skill counters in the dashboard badge
    const badge = document.getElementById('evolution-count');
    if (badge) {
        const prev = badge.textContent.match(/(\d+) Genes/);
        const prevGenes = prev ? parseInt(prev[1]) : 0;
        const prevSkills = badge.textContent.match(/(\d+) Skills/);
        const prevSkillCount = prevSkills ? parseInt(prevSkills[1]) : 0;
        badge.textContent = `${prevGenes + gene_count} Genes · ${prevSkillCount + skill_count} Skills`;
    }

    // Add to evolution timeline
    if (typeof addTimelineEvent === 'function') {
        addTimelineEvent('gene', `Evolution 完成`, summary);
    }

    showToast(`Evolution 完成: ${gene_count} Gene${gene_count !== 1 ? 's' : ''}, ${skill_count} Skill${skill_count !== 1 ? 's' : ''}`);
}

function escapeHtml(str) {
    return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

/**
 * Handle a `reflection:complete` EventBus event (background reflection finished).
 * Called by websocket.js. Renders into the evolution timeline.
 * @param {object} payload - { reflection: {...}, improvement: {...} }
 */
function handleReflectionComplete(payload) {
    const reflection = payload.reflection || {};
    const improvement = payload.improvement || {};

    // Show a dismissable notification
    showToast('反思完成');

    // Add to evolution timeline
    if (typeof addTimelineEvent === 'function') {
        addTimelineEvent('reflection', '反思完成', reflection.summary || '');
    }

    // If main_new.js addReflectionMessage exists, call it directly
    if (typeof window._addReflectionMessage === 'function') {
        window._addReflectionMessage(reflection, improvement);
    }
}

// Expose on window for cross-module calls
window.showToast = showToast;
window.handleEvolutionNotify = handleEvolutionNotify;
window.handleReflectionComplete = handleReflectionComplete;
