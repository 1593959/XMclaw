// XMclaw — Buddy Mascot (B-108, free-code buddy/Companion parity)
//
// A small floating 🦞 in the bottom-right corner. Idles, blinks every
// few seconds, occasionally "thinks" with dots, and bounces when
// petted. Pure decoration — no functional impact, but XMclaw's
// branding is the lobster claw emoji and not having it visible
// anywhere in the UI was a missed opportunity.
//
// Toggle via right-click → "关闭" or programmatically by writing
// localStorage["xmc-buddy-enabled"] = "0". Default ON.
//
// Free-code's buddy was a full pixel sprite system with rarity tiers
// (common / rare / legendary), species (otter / panda / ember / …),
// hat slots, names. We just use the brand emoji here — the point is
// having a small companion in the UI, not the gacha mechanics.

const { h } = window.__xmc.preact;
const { useState, useEffect, useRef } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

const _PET_KEY = "xmc-buddy-enabled";

// Idle frames — the lobster cycles through these subtly so it feels
// alive without becoming distracting. The "blink" frame momentarily
// switches to the eyes-closed form.
const FRAMES = ["🦞", "🦞", "🦞", "🦞", "🦐", "🦞"];
const BLINK_INTERVAL_MS = 4500;
const FRAME_TICK_MS = 350;

export function BuddyMascot() {
  // Honour user preference (default ON).
  const initialEnabled = (() => {
    try {
      return localStorage.getItem(_PET_KEY) !== "0";
    } catch (_) { return true; }
  })();
  const [enabled, setEnabled] = useState(initialEnabled);
  const [frameIdx, setFrameIdx] = useState(0);
  const [hearts, setHearts] = useState(0);  // bumped on pet to retrigger
  const [bouncing, setBouncing] = useState(false);
  const [showMenu, setShowMenu] = useState(false);

  // Frame animation tick.
  useEffect(() => {
    if (!enabled) return undefined;
    const id = setInterval(() => {
      setFrameIdx((i) => (i + 1) % FRAMES.length);
    }, FRAME_TICK_MS);
    return () => clearInterval(id);
  }, [enabled]);

  // Periodic blink — switch FRAMES briefly via a side state.
  const [blinkAt, setBlinkAt] = useState(0);
  useEffect(() => {
    if (!enabled) return undefined;
    const id = setInterval(() => {
      setBlinkAt(Date.now());
      // Blink lasts 200ms; frame counter naturally takes over again.
    }, BLINK_INTERVAL_MS);
    return () => clearInterval(id);
  }, [enabled]);

  const onPet = () => {
    setBouncing(true);
    setHearts((n) => n + 1);
    setTimeout(() => setBouncing(false), 500);
  };

  const onContextMenu = (e) => {
    e.preventDefault();
    setShowMenu(true);
  };

  const onDisable = () => {
    setEnabled(false);
    setShowMenu(false);
    try { localStorage.setItem(_PET_KEY, "0"); } catch (_) { /* no-op */ }
  };

  if (!enabled) return null;

  // Are we in the middle of a blink?
  const inBlink = blinkAt > 0 && (Date.now() - blinkAt) < 200;
  const display = inBlink ? "—" : FRAMES[frameIdx];

  return html`
    <div
      class=${"xmc-buddy" + (bouncing ? " is-bouncing" : "")}
      role="button"
      aria-label="XMclaw 吉祥物 — 右键关闭"
      tabindex="0"
      onClick=${onPet}
      onContextMenu=${onContextMenu}
      title="XMclaw 🦞 — 点一下抚摸，右键关闭"
    >
      <span class="xmc-buddy__sprite" aria-hidden="true">${display}</span>
      ${hearts > 0 && bouncing
        ? html`<span class="xmc-buddy__heart">♥</span>`
        : null}
      ${showMenu ? html`
        <div class="xmc-buddy__menu" onClick=${(e) => e.stopPropagation()}>
          <button type="button" class="xmc-buddy__menu-item" onClick=${onDisable}>
            关闭吉祥物
          </button>
          <button type="button" class="xmc-buddy__menu-item" onClick=${() => setShowMenu(false)}>
            取消
          </button>
        </div>
      ` : null}
    </div>
  `;
}
