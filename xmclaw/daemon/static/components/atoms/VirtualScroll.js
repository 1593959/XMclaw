// XMclaw VirtualScroll — lightweight virtualised container.
// Only renders items visible in the viewport + a small buffer.
// Usage: <VirtualScroll items={arr} rowHeight={32} renderRow={fn} />

const { h } = window.__xmc.preact;
const { useState, useRef, useEffect, useCallback } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

export function VirtualScroll({ items = [], rowHeight = 32, overscan = 10, renderRow, keyFn }) {
  const containerRef = useRef(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [containerHeight, setContainerHeight] = useState(0);

  const totalHeight = items.length * rowHeight;

  const onScroll = useCallback(() => {
    if (containerRef.current) {
      setScrollTop(containerRef.current.scrollTop);
    }
  }, []);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const ro = new ResizeObserver(([entry]) => {
      setContainerHeight(entry.contentRect.height);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const startIdx = Math.max(0, Math.floor(scrollTop / rowHeight) - overscan);
  const endIdx = Math.min(items.length, Math.ceil((scrollTop + containerHeight) / rowHeight) + overscan);
  const visibleItems = items.slice(startIdx, endIdx);
  const offsetY = startIdx * rowHeight;

  return html`
    <div ref=${containerRef} class="virtual-scroll" onScroll=${onScroll}
         style=${{ height: "100%", overflow: "auto" }}>
      <div style=${{ height: totalHeight + "px", position: "relative" }}>
        <div style=${{ position: "absolute", top: offsetY + "px", width: "100%" }}>
          ${visibleItems.map((item, i) => {
            const idx = startIdx + i;
            return html`<div key=${keyFn ? keyFn(item, idx) : idx} style=${{ height: rowHeight + "px" }}>
              ${renderRow(item, idx)}
            </div>`;
          })}
        </div>
      </div>
    </div>
  `;
}
