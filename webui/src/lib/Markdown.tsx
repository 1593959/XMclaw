// 富文本渲染 — agent 陈述的 markdown 全功能渲染。
// 用户 2026-06-12 反馈：旧骨架把表格糊成管道符纯文本，渲染质量必须
// 当一等公民。GFM（表格/删除线/任务列表）+ 代码高亮 + 媒体 URL 补 token。

import { useState, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import { resolveMediaUrl } from "./api";
import { isSafeMarkdownHref, isSafeMarkdownImageUrl } from "./artifactSecurity";

// 代码块带复制按钮（agent 频繁输出代码，高频需求）。
function CodeBlock({ children }: { children?: ReactNode }) {
  const [copied, setCopied] = useState(false);
  function copy(e: React.MouseEvent) {
    const pre = (e.currentTarget as HTMLElement).parentElement?.querySelector("code");
    const text = pre?.textContent || "";
    navigator.clipboard?.writeText(text).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  }
  return (
    <pre className="group relative">
      <button
        onClick={copy}
        className="absolute top-2 right-2 text-[10.5px] px-2 py-0.5 rounded border border-mc-border bg-mc-panel text-mc-faint hover:text-mc-accent cursor-pointer opacity-0 group-hover:opacity-100 transition-opacity"
      >
        {copied ? "已复制" : "复制"}
      </button>
      {children}
    </pre>
  );
}

export default function Markdown({ text }: { text: string }) {
  return (
    <div className="mc-md min-w-0">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[[rehypeHighlight, { detect: false, ignoreMissing: true }]]}
        components={{
          img: ({ src, alt }) => {
            const raw = typeof src === "string" ? src : "";
            if (!isSafeMarkdownImageUrl(raw)) {
              return (
                <span className="inline-block rounded border border-dashed border-mc-border px-1.5 py-0.5 text-[11px] text-mc-faint">
                  image blocked
                </span>
              );
            }
            return (
              <img
                src={resolveMediaUrl(raw)}
                alt={alt || ""}
                className="max-w-md rounded border border-mc-border my-1"
              />
            );
          },
          a: ({ href, children }) => {
            const raw = typeof href === "string" ? href : "";
            if (!isSafeMarkdownHref(raw)) {
              return <span className="text-mc-faint">{children}</span>;
            }
            return (
              <a href={raw} target="_blank" rel="noreferrer" className="text-mc-accent underline">
                {children}
              </a>
            );
          },
          pre: ({ children }) => <CodeBlock>{children}</CodeBlock>,
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
