// XMclaw — EvolutionPage (Epic #24 Phase 1 placeholder).
//
// Old page (B-126 family) was a per-day xm-auto-evo skill_promoted /
// candidate / rolled_back ledger backed by `/api/v2/auto_evo/*` and
// `~/.xmclaw/auto_evo/skills/`. That whole subsystem was torn out in
// Phase 1 because it ran without HonestGrader gating — exactly the
// "agent always thinks it performed well" failure mode XMclaw exists
// to fix.
//
// Phase 2 brings this page back as a real evolution dashboard backed by
// `EvolutionAgent` audit logs + `EventBus` GRADER_VERDICT events, all
// of them flowing through the evidence-gated SkillRegistry door.
// Until then, this is a clear "重做中" placeholder so users (and the
// Sidebar accent dot) don't navigate into a 404.

const { h } = window.__xmc.preact;
const html = window.__xmc.htm.bind(h);

export function EvolutionPage({ token: _token }) {
  return html`
    <section class="xmc-h-page" aria-labelledby="evolution-title">
      <header class="xmc-h-page__header">
        <div class="xmc-h-page__heading">
          <h2 id="evolution-title" class="xmc-h-page__title">进化</h2>
          <p class="xmc-h-page__subtitle">
            自主进化重做中 · Epic #24 Phase 1（拆 + 接电源）已完成 · Phase 2 将上线新的进化面板。
          </p>
        </div>
      </header>

      <div class="xmc-h-page__body">
        <div style="max-width:760px;margin:1rem auto;padding:1.4rem 1.6rem;border:1px solid var(--color-border);border-radius:8px;background:color-mix(in srgb, var(--midground) 4%, transparent);line-height:1.7">
          <p style="margin:0 0 .8rem;font-size:1rem">
            <strong>原 Evolution 页（基于 xm-auto-evo Node 子系统）已下线。</strong>
          </p>
          <p style="margin:0 0 .8rem;font-size:.92rem;opacity:.9">
            原因：旧版面展示的"自动进化"产物没有经过 HonestGrader 把关 ——
            和 Hermes "agent 总以为自己干得不错" 是同款失败模式。
            重做后的进化层会让所有 SKILL.md 候选都过证据闸门后才能 promote。
          </p>
          <p style="margin:0 0 .4rem;font-size:.92rem">
            <strong>当前可用的入口</strong>：
          </p>
          <ul style="margin:.2rem 0 .8rem 1.2rem;font-size:.88rem">
            <li><a href="/skills">/skills</a> — 看 SkillRegistry 注册的所有技能 + 手动 promote / rollback</li>
            <li><a href="/sessions">/sessions</a> — 每个 session 的事件流（grader_verdict / skill_promoted / skill_rolled_back）</li>
            <li><a href="/journal">/journal</a> — 每日 journal（Phase 2 起接 LLM 复盘）</li>
          </ul>
          <p style="margin:0;font-size:.85rem;opacity:.7">
            Phase 2 进度跟踪在
            <code>docs/DEV_ROADMAP.md</code> Epic #24。
          </p>
        </div>
      </div>
    </section>
  `;
}
