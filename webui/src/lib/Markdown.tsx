// 富文本渲染 — agent 陈述的 markdown 全功能渲染。
// 用户 2026-06-12 反馈：旧骨架把表格糊成管道符纯文本，渲染质量必须
// 当一等公民。GFM（表格/删除线/任务列表）+ 代码高亮 + 媒体 URL 补 token。

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import { resolveMediaUrl } from "./api";

export default function Markdown({ text }: { text: string }) {
  return (
    <div className="mc-md min-w-0">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[[rehypeHighlight, { detect: false, ignoreMissing: true }]]}
        components={{
          img: ({ src, alt }) => (
            <img
              src={resolveMediaUrl(typeof src === "string" ? src : "")}
              alt={alt || ""}
              className="max-w-md rounded border border-mc-border my-1"
            />
          ),
          a: ({ href, children }) => (
            <a href={href} target="_blank" rel="noreferrer" className="text-mc-accent underline">
              {children}
            </a>
          ),
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
