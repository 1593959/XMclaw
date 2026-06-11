// 右栏常驻工作区 — M1 骨架：四标签框架 + 文件树（复用
// session_workspaces API）。Diff 渲染 / 终端 / 预览在 10.M2.4 填充。

import { useEffect, useState } from "react";
import { useApp } from "../store/app";
import { apiGet } from "../lib/api";

const TABS = ["Diff", "文件", "终端", "预览"] as const;
type Tab = (typeof TABS)[number];

interface TreeNode {
  path: string;
  type?: string;
  size?: number;
}

export default function WorkspacePanel() {
  const [tab, setTab] = useState<Tab>("文件");
  const token = useApp((s) => s.token);
  const sid = useApp((s) => s.sid);
  const [tree, setTree] = useState<TreeNode[]>([]);

  useEffect(() => {
    if (!token || !sid || tab !== "文件") return;
    apiGet<{ tree?: TreeNode[]; files?: TreeNode[] }>(
      `/api/v2/session_workspaces/${encodeURIComponent(sid)}/tree`,
      token,
    )
      .then((d) => setTree(d?.tree || d?.files || []))
      .catch(() => setTree([]));
  }, [token, sid, tab]);

  return (
    <aside className="w-72 border-l border-mc-border bg-mc-panel hidden xl:flex flex-col shrink-0">
      <div className="flex gap-1 px-2 pt-2 border-b border-mc-border shrink-0">
        {TABS.map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={
              "text-xs px-3 py-1.5 rounded-t-md cursor-pointer " +
              (tab === t
                ? "bg-mc-panel2 text-mc-text font-medium"
                : "text-mc-faint hover:text-mc-muted")
            }
          >
            {t}
          </button>
        ))}
      </div>
      <div className="flex-1 overflow-y-auto p-3">
        {tab === "文件" &&
          (tree.length > 0 ? (
            <ul className="space-y-0.5">
              {tree.map((n) => (
                <li key={n.path} className="text-xs font-mono text-mc-muted truncate" title={n.path}>
                  {n.path}
                </li>
              ))}
            </ul>
          ) : (
            <div className="text-xs text-mc-faint">工作区暂无文件 — agent 产出后实时出现</div>
          ))}
        {tab !== "文件" && (
          <div className="text-xs text-mc-faint">{tab} 面板在 M2 接入（10.M2.4）</div>
        )}
      </div>
    </aside>
  );
}
