// XMclaw MessageSearch — inline search bar for conversation messages.
// Debounced search across current session messages.

const { h } = window.__xmc.preact;
const { useState, useCallback, useRef } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

export function MessageSearch({ messages = [], onSelectResult }) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState([]);
  const [focused, setFocused] = useState(false);
  const timerRef = useRef(null);

  const doSearch = useCallback((q) => {
    if (!q || q.length < 2) { setResults([]); return; }
    const ql = q.toLowerCase();
    const hits = [];
    for (let i = messages.length - 1; i >= 0; i--) {
      const m = messages[i];
      const content = (m.content || "").toLowerCase();
      const idx = content.indexOf(ql);
      if (idx !== -1) {
        hits.push({
          msg: m,
          idx: i,
          snippet: (m.content || "").slice(Math.max(0, idx - 30), idx + ql.length + 60),
          matchStart: Math.min(30, idx),
        });
      }
      if (hits.length >= 20) break;
    }
    setResults(hits);
  }, [messages]);

  const onInput = useCallback((e) => {
    const v = e.target.value;
    setQuery(v);
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => doSearch(v), 200);
  }, [doSearch]);

  const onKeyDown = useCallback((e) => {
    if (e.key === "Escape") { setQuery(""); setResults([]); setFocused(false); }
  }, []);

  return html`
    <div class="message-search">
      <input
        class="message-search__input"
        type="text"
        placeholder="Search messages… (Esc to close)"
        value=${query}
        onInput=${onInput}
        onKeyDown=${onKeyDown}
        onFocus=${() => setFocused(true)}
        onBlur=${() => setTimeout(() => setFocused(false), 200)}
      />
      ${focused && results.length > 0 ? html`
        <div class="message-search__results">
          ${results.map((r, i) => html`
            <div key=${i} class="message-search__result"
                 onMouseDown=${(e) => { e.preventDefault(); onSelectResult?.(r.idx); }}>
              <span class="message-search__role">${r.msg.role}</span>
              <span class="message-search__snippet">…${r.snippet}…</span>
            </div>
          `)}
        </div>
      ` : null}
    </div>
  `;
}
