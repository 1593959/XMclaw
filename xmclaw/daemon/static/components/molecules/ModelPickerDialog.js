// XMclaw — ModelPickerDialog 1:1 port of hermes-agent ModelPickerDialog.tsx
//
// Two-stage modal:
//   Stage 1: pick a provider (left column)
//   Stage 2: pick a model within that provider (right column)
// Footer: persist-globally checkbox + Cancel + Switch buttons
//
// Hermes wires through gw.request("model.options"). XMclaw uses
// /api/v2/llm/profiles which already returns LLMProfile objects with
// {id, label, provider, model}. We group those by
// provider into the same {providers: [{slug, name, models[],
// is_current}]} shape ModelPickerDialog expects.

const { h } = window.__xmc.preact;
const { useState, useEffect, useMemo } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { apiGet } from "../../lib/api.js";
import { toast } from "../../lib/toast.js";

function Icon({ d, className }) {
  return html`
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"
         class=${"xmc-icon " + (className || "")} aria-hidden="true">
      <path d=${d} />
    </svg>
  `;
}

const I_SEARCH = "M11 17a6 6 0 1 0 0-12 6 6 0 0 0 0 12zM21 21l-4.3-4.3";
const I_X      = "M18 6 6 18 M6 6l12 12";
const I_CHECK  = "M5 13l5 5L20 7";
const I_LOADER = "M21 12a9 9 0 1 1-6.219-8.56";

function CurrentTag() {
  return html`
    <span class="xmc-h-mpd__current-tag">
      <${Icon} d=${I_CHECK} />
      current
    </span>
  `;
}

// ── Provider column ─────────────────────────────────────────────

function ProviderColumn({ loading, error, providers, total, selectedSlug, query, onSelect }) {
  if (loading) {
    return html`
      <div class="xmc-h-mpd__provcol">
        <div class="xmc-h-mpd__col-loading">
          <${Icon} d=${I_LOADER} className="xmc-h-mpd__spin" />
          载入中…
        </div>
      </div>
    `;
  }
  if (error) {
    return html`
      <div class="xmc-h-mpd__provcol">
        <div class="xmc-h-mpd__col-error">${error}</div>
      </div>
    `;
  }
  return html`
    <div class="xmc-h-mpd__provcol">
      ${providers.length === 0
        ? html`<div class="xmc-h-mpd__col-empty">${query ? "无匹配" : (total === 0 ? "未配置任何 provider" : "无匹配")}</div>`
        : providers.map((p) => {
          const active = p.slug === selectedSlug;
          return html`
            <button
              key=${p.slug}
              type="button"
              class=${"xmc-h-mpd__provrow " + (active ? "is-active" : "")}
              onClick=${() => onSelect(p.slug)}
            >
              <div class="xmc-h-mpd__provrow-main">
                <div class="xmc-h-mpd__provrow-titleline">
                  <span class="xmc-h-mpd__provrow-name">${p.name}</span>
                  ${p.is_current ? html`<${CurrentTag} />` : null}
                </div>
                <div class="xmc-h-mpd__provrow-meta">
                  ${p.slug} · ${(p.models || []).length} 模型
                </div>
              </div>
            </button>
          `;
        })}
    </div>
  `;
}

// ── Model column ─────────────────────────────────────────────────

function ModelColumn({ provider, models, allModels, selectedModel, currentModel, currentProviderSlug, onSelect, onConfirm }) {
  if (!provider) {
    return html`
      <div class="xmc-h-mpd__modelcol">
        <div class="xmc-h-mpd__col-empty">选择左侧 provider 查看可用模型</div>
      </div>
    `;
  }
  return html`
    <div class="xmc-h-mpd__modelcol">
      <div class="xmc-h-mpd__modelcol-head">
        <h3 class="xmc-h-mpd__modelcol-title">${provider.name}</h3>
        <span class="xmc-h-mpd__modelcol-count">
          ${models.length}/${allModels.length} 模型
        </span>
      </div>
      <div class="xmc-h-mpd__modellist">
        ${models.length === 0
          ? html`<div class="xmc-h-mpd__col-empty">这个 provider 还没列任何模型</div>`
          : models.map((m) => {
            const active = m === selectedModel;
            const isCur =
              m === currentModel && provider.slug === currentProviderSlug;
            return html`
              <button
                key=${m}
                type="button"
                class=${"xmc-h-mpd__modelrow " + (active ? "is-active" : "")}
                onClick=${() => onSelect(m)}
                onDblClick=${() => onConfirm(m)}
              >
                <code class="xmc-h-mpd__modelname">${m}</code>
                ${isCur ? html`<${CurrentTag} />` : null}
                ${active ? html`<${Icon} d=${I_CHECK} className="xmc-h-mpd__modelcheck" /> ` : null}
              </button>
            `;
          })}
      </div>
    </div>
  `;
}

// ── Main component ──────────────────────────────────────────────

export function ModelPickerDialog({ token, sessionId, currentProfileId, onClose, onApply }) {
  const [profiles, setProfiles] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [query, setQuery] = useState("");
  const [persistGlobal, setPersistGlobal] = useState(false);
  const [selectedSlug, setSelectedSlug] = useState("");
  const [selectedModel, setSelectedModel] = useState("");

  // Fetch profiles + group by provider.
  useEffect(() => {
    let cancelled = false;
    apiGet("/api/v2/llm/profiles", token)
      .then((d) => {
        if (cancelled) return;
        const list = d.profiles || [];
        setProfiles(list);
        const byProv = new Map();
        for (const p of list) {
          const slug = p.provider || "unknown";
          const bucket = byProv.get(slug) || { slug, name: slug, models: [], profileIds: [] };
          if (!bucket.models.includes(p.model)) bucket.models.push(p.model);
          bucket.profileIds.push(p.id);
          if (p.id === currentProfileId) bucket.is_current = true;
          byProv.set(slug, bucket);
        }
        const cur = list.find((p) => p.id === currentProfileId);
        const initialSlug = (cur && cur.provider) || (list[0] && list[0].provider) || "";
        setSelectedSlug(initialSlug);
        setSelectedModel(cur ? cur.model : "");
        setLoading(false);
      })
      .catch((e) => {
        if (!cancelled) {
          setError(String(e.message || e));
          setLoading(false);
        }
      });
    return () => { cancelled = true; };
  }, [token, currentProfileId]);

  // Esc closes.
  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") { e.preventDefault(); onClose(); } };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const providers = useMemo(() => {
    const byProv = new Map();
    for (const p of profiles) {
      const slug = p.provider || "unknown";
      const bucket = byProv.get(slug) || { slug, name: slug, models: [], profileIds: [] };
      if (!bucket.models.includes(p.model)) bucket.models.push(p.model);
      bucket.profileIds.push(p.id);
      if (p.id === currentProfileId) bucket.is_current = true;
      byProv.set(slug, bucket);
    }
    return Array.from(byProv.values());
  }, [profiles, currentProfileId]);

  const needle = query.trim().toLowerCase();
  const filteredProviders = useMemo(() => {
    if (!needle) return providers;
    return providers.filter((p) =>
      p.name.toLowerCase().includes(needle) ||
      p.slug.toLowerCase().includes(needle) ||
      (p.models || []).some((m) => m.toLowerCase().includes(needle))
    );
  }, [providers, needle]);

  const selectedProvider = useMemo(
    () => providers.find((p) => p.slug === selectedSlug) || null,
    [providers, selectedSlug],
  );

  const filteredModels = useMemo(() => {
    if (!selectedProvider) return [];
    if (!needle) return selectedProvider.models;
    return selectedProvider.models.filter((m) => m.toLowerCase().includes(needle));
  }, [selectedProvider, needle]);

  const currentProfile = profiles.find((p) => p.id === currentProfileId);
  const currentModel = currentProfile ? currentProfile.model : "";
  const currentProvSlug = currentProfile ? currentProfile.provider : "";

  const canConfirm = !!selectedSlug && !!selectedModel;

  const confirm = () => {
    const target = profiles.find(
      (p) => p.provider === selectedSlug && p.model === selectedModel
    );
    if (!target) {
      toast.error("找不到对应 profile（可能 profile 列表已变化，请重打开）");
      return;
    }
    onApply && onApply({
      profile_id: target.id,
      model: selectedModel,
      provider: selectedSlug,
      persist_global: persistGlobal,
    });
    onClose();
  };

  return html`
    <div class="xmc-h-dialog__backdrop" onClick=${onClose}>
      <div
        class="xmc-h-dialog xmc-h-mpd"
        role="dialog"
        aria-modal="true"
        aria-labelledby="mpd-title"
        onClick=${(e) => e.stopPropagation()}
      >
        <header class="xmc-h-mpd__head">
          <h2 id="mpd-title" class="xmc-h-mpd__title">切换模型</h2>
          <p class="xmc-h-mpd__subtitle">
            current: <code>${currentModel || "(未知)"}</code>
            ${currentProvSlug ? html` · <code>${currentProvSlug}</code>` : null}
          </p>
          <button
            type="button"
            class="xmc-h-mpd__close"
            onClick=${onClose}
            aria-label="close"
          ><${Icon} d=${I_X} /></button>
        </header>

        <div class="xmc-h-mpd__searchbar">
          <span class="xmc-h-mpd__searchicon"><${Icon} d=${I_SEARCH} /></span>
          <input
            autofocus
            class="xmc-h-input"
            placeholder="按 provider / model 名筛选…"
            value=${query}
            onInput=${(e) => setQuery(e.target.value)}
          />
        </div>

        <div class="xmc-h-mpd__cols">
          <${ProviderColumn}
            loading=${loading}
            error=${error}
            providers=${filteredProviders}
            total=${providers.length}
            selectedSlug=${selectedSlug}
            query=${needle}
            onSelect=${(slug) => { setSelectedSlug(slug); setSelectedModel(""); }}
          />
          <${ModelColumn}
            provider=${selectedProvider}
            models=${filteredModels}
            allModels=${selectedProvider?.models || []}
            selectedModel=${selectedModel}
            currentModel=${currentModel}
            currentProviderSlug=${currentProvSlug}
            onSelect=${setSelectedModel}
            onConfirm=${(m) => { setSelectedModel(m); setTimeout(confirm, 0); }}
          />
        </div>

        <footer class="xmc-h-mpd__foot">
          <label class="xmc-h-mpd__persist">
            <input
              type="checkbox"
              checked=${persistGlobal}
              onChange=${(e) => setPersistGlobal(e.target.checked)}
            />
            <span>持久化（否则只对本会话生效）</span>
          </label>
          <div class="xmc-h-mpd__foot-buttons">
            <button type="button" class="xmc-h-btn" onClick=${onClose}>取消</button>
            <button
              type="button"
              class="xmc-h-btn xmc-h-btn--primary"
              onClick=${confirm}
              disabled=${!canConfirm}
            >切换</button>
          </div>
        </footer>
      </div>
    </div>
  `;
}
