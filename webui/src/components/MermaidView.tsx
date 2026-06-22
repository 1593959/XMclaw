import { useEffect, useRef, useState } from "react";

// 把 canvas 的 ``mermaid`` 产物真正渲染成图（之前 WorkspacePanel 直接把 graph TD
// 源码塞进 iframe，只显示裸文本 = "丑/排版乱"）。mermaid 体积较大，用动态
// import 让 Vite 自动 code-split — 只有真出现 mermaid 产物时才加载这块，
// 主包不膨胀、运行时零 CDN（mermaid 已打进 bundle）。内容来自 LLM，用
// securityLevel: "strict" 禁止标签内脚本/HTML 注入。
let _inited = false;

export default function MermaidView({ content }: { content: string }) {
  const [svg, setSvg] = useState("");
  const [err, setErr] = useState("");
  const idRef = useRef(`mmd-${Math.random().toString(36).slice(2)}`);
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const mermaid = (await import("mermaid")).default;
        if (!_inited) {
          mermaid.initialize({
            startOnLoad: false,
            securityLevel: "strict",
            theme: "dark",
            fontFamily: "inherit",
          });
          _inited = true;
        }
        const { svg } = await mermaid.render(idRef.current, content.trim());
        if (!cancelled) {
          setSvg(svg);
          setErr("");
        }
      } catch (e) {
        if (!cancelled) setErr(String((e as Error)?.message || e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [content]);

  if (err) {
    return (
      <pre className="text-[11px] font-mono text-mc-err whitespace-pre-wrap break-all max-h-72 overflow-y-auto border border-mc-border rounded p-2">
        {`图表渲染失败：${err}\n\n${content}`}
      </pre>
    );
  }
  if (!svg) {
    return <div className="text-xs text-mc-faint p-3">渲染图表中…</div>;
  }
  return (
    <div
      className="w-full max-h-[28rem] overflow-auto rounded border border-mc-border bg-white p-2 [&_svg]:max-w-full [&_svg]:h-auto"
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  );
}
