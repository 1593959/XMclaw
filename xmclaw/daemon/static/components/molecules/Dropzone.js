// XMclaw — Dropzone (molecule)
//
// Dashed-border drag-and-drop upload area. Highlights on dragover
// (purple border + faint purple background). Displays an icon,
// prompt text, and accepted-format hints.
//
// Props:
//   onFiles    function(File[]) – called when files are dropped
//   accept     string          – mime-type filter (e.g. "image/*")
//   formats    string          – hint text shown below (e.g. "PNG, JPG, GIF")
//   prompt     string          – main label (default: "拖放文件到此处上传")
//   icon       string          – emoji or text icon (default: "📁")

const { h } = window.__xmc.preact;
const { useState, useCallback } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

export function Dropzone({
  onFiles,
  accept,
  formats = "",
  prompt = "拖放文件到此处上传",
  icon = "📁",
}) {
  const [dragover, setDragover] = useState(false);

  const handleDragOver = useCallback((e) => {
    e.preventDefault();
    setDragover(true);
  }, []);

  const handleDragLeave = useCallback((e) => {
    e.preventDefault();
    setDragover(false);
  }, []);

  const handleDrop = useCallback((e) => {
    e.preventDefault();
    setDragover(false);
    const files = Array.from(e.dataTransfer.files);
    if (files.length && onFiles) {
      onFiles(files);
    }
  }, [onFiles]);

  const cls = ["nb-dropzone", dragover ? "dragover" : ""].filter(Boolean).join(" ");

  return html`
    <div
      class=${cls}
      onDragOver=${handleDragOver}
      onDragLeave=${handleDragLeave}
      onDrop=${handleDrop}
      role="button"
      tabindex="0"
      aria-label=${prompt}
    >
      <div class="nb-dropzone__icon">${icon}</div>
      <div class="nb-dropzone__text">${prompt}</div>
      ${formats
        ? html`<div class="nb-dropzone__hint">支持格式：${formats}</div>`
        : null}
    </div>
  `;
}
