// Markdown 懒加载壳（10.M2.5 code-split）：react-markdown + hljs 约占
// 主 bundle 60%，拆成按需 chunk。fallback 用纯文本 pre，闪现半帧即换。

import { lazy, Suspense } from "react";

const Inner = lazy(() => import("../lib/Markdown"));

export default function LazyMarkdown({ text }: { text: string }) {
  return (
    <Suspense
      fallback={<pre className="text-[13px] whitespace-pre-wrap break-words font-sans m-0">{text}</pre>}
    >
      <Inner text={text} />
    </Suspense>
  );
}
