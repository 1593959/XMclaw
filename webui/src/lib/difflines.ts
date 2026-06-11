// diff 行构建 — EditCard（§2.3.1 file_edit 内联高亮 diff 卡）的数据层。
// 两个来源：① file_edit 的 old_string/new_string args → jsdiff 现算；
// ② git unified diff 文本（session_workspaces /diff API）→ 解析着色。

import { diffLines } from "diff";

export interface DiffLine {
  type: "add" | "del" | "ctx" | "meta";
  text: string;
  lineNo: number | null;
}

export interface DiffStat {
  adds: number;
  dels: number;
}

// ① old/new 文本对 → 行级 diff。
export function buildDiffFromStrings(oldStr: string, newStr: string): { lines: DiffLine[]; stat: DiffStat } {
  const parts = diffLines(oldStr, newStr);
  const lines: DiffLine[] = [];
  let adds = 0;
  let dels = 0;
  let lineNo = 1;
  for (const p of parts) {
    const rows = p.value.split("\n");
    if (rows[rows.length - 1] === "") rows.pop();
    for (const row of rows) {
      if (p.added) {
        lines.push({ type: "add", text: row, lineNo });
        adds += 1;
        lineNo += 1;
      } else if (p.removed) {
        lines.push({ type: "del", text: row, lineNo: null });
        dels += 1;
      } else {
        lines.push({ type: "ctx", text: row, lineNo });
        lineNo += 1;
      }
    }
  }
  return { lines, stat: { adds, dels } };
}

// ② git unified diff 文本 → 行列表（行号取新文件侧）。
export function parseUnifiedDiff(diffText: string): { lines: DiffLine[]; stat: DiffStat } {
  const lines: DiffLine[] = [];
  let adds = 0;
  let dels = 0;
  let newLineNo: number | null = null;
  for (const row of diffText.split("\n")) {
    if (row.startsWith("@@")) {
      const m = /\+(\d+)/.exec(row);
      newLineNo = m ? parseInt(m[1], 10) : null;
      lines.push({ type: "meta", text: row, lineNo: null });
    } else if (row.startsWith("+++") || row.startsWith("---") || row.startsWith("diff ") || row.startsWith("index ")) {
      lines.push({ type: "meta", text: row, lineNo: null });
    } else if (row.startsWith("+")) {
      lines.push({ type: "add", text: row.slice(1), lineNo: newLineNo });
      if (newLineNo != null) newLineNo += 1;
      adds += 1;
    } else if (row.startsWith("-")) {
      lines.push({ type: "del", text: row.slice(1), lineNo: null });
      dels += 1;
    } else {
      lines.push({ type: "ctx", text: row.startsWith(" ") ? row.slice(1) : row, lineNo: newLineNo });
      if (newLineNo != null) newLineNo += 1;
    }
  }
  return { lines, stat: { adds, dels } };
}

// 超长 diff 默认折叠中段：保留头尾各 keep 行。
export function collapseMiddle(lines: DiffLine[], keep = 20): { head: DiffLine[]; hidden: DiffLine[]; tail: DiffLine[] } {
  if (lines.length <= keep * 2 + 6) return { head: lines, hidden: [], tail: [] };
  return {
    head: lines.slice(0, keep),
    hidden: lines.slice(keep, lines.length - keep),
    tail: lines.slice(lines.length - keep),
  };
}
