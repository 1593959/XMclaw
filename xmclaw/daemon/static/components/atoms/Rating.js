// XMclaw — Rating atom
//
// 5-level emoji rating: 👍 ⭐ ❤️ 🔥 🚀
// Click to select, hover to preview. Emits the selected level (1-5)
// via onChange.
//
// Props:
//   value     number   – currently selected level (0 = none)
//   onChange  function – called with the new level (1-5) on click
//   disabled  boolean  – non-interactive when true

const { h } = window.__xmc.preact;
const { useState } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

const EMOJIS = ["👍", "⭐", "❤️", "🔥", "🚀"];

export function Rating({ value = 0, onChange, disabled = false }) {
  const [hover, setHover] = useState(0);

  const activeLevel = hover || value || 0;

  return html`
    <div
      class="nb-rating"
      role="radiogroup"
      aria-label="评分"
      onMouseLeave=${() => setHover(0)}
    >
      ${EMOJIS.map((emoji, i) => {
        const level = i + 1;
        const isActive = level <= activeLevel;
        return html`
          <button
            key=${level}
            type="button"
            class=${"nb-rating__btn " + (isActive ? "active" : "")}
            role="radio"
            aria-checked=${value === level}
            aria-label="${level} 星"
            disabled=${disabled}
            onMouseEnter=${() => setHover(level)}
            onClick=${() => onChange?.(level)}
          >
            ${emoji}
          </button>
        `;
      })}
    </div>
  `;
}
