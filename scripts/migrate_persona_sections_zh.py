"""一次性迁移：把 persona 文件里 daemon 自动管理的段落标题 + 标记
从英文改成中文（配合 buckets.py 段名中文化）。

为什么需要：v2_renderer 用「## 标题」派生 XMC-AUTO-EXTRACTED:<slug> 标记
来定位/替换自动段。直接把 buckets 的 section 改中文后，渲染器找不到旧
英文标记 → 会追加新中文段、旧英文段留着变重复。本脚本把现有文件里的
旧英文标题行 + 标记 slug 原地改成中文，内容保留、幂等（旧串找不到即跳过）。

用法：python scripts/migrate_persona_sections_zh.py [--dry-run]
扫描 ~/.xmclaw/persona/profiles/*/ 下所有 .md。
"""
from __future__ import annotations

import sys
from pathlib import Path


def _slug(header: str) -> str:
    """与 v2_renderer._section_markers / buckets.safe_section_slug 完全一致。"""
    s = header.lstrip("# ").strip()
    return "".join(c if c.isalnum() else "-" for c in s).strip("-")


# 旧英文标题(不含 "## ") → 新中文标题。按 (文件, 旧标题) 区分，因为
# "Auto-extracted" 在 IDENTITY/SOUL/LEARNING 三处含义不同。
RENAMES: dict[str, list[tuple[str, str]]] = {
    "IDENTITY.md": [("Auto-extracted", "自动提取（身份）")],
    "USER.md": [
        ("Auto-identity", "自动识别（用户身份）"),
        ("Auto-extracted preferences", "自动提取（偏好）"),
    ],
    "SOUL.md": [("Auto-extracted", "自动提取（价值观）")],
    "AGENTS.md": [("Workflows", "工作流")],
    "TOOLS.md": [("Tool quirks", "工具坑与窍门")],
    "LEARNING.md": [("Auto-extracted", "自动提取（规则）")],
    "MEMORY.md": [
        ("Failure Modes", "失败模式"),
        ("Project facts", "项目事实"),
        ("Active commitments", "进行中的承诺"),
        ("Other facts (recent)", "近期其它事实"),
    ],
}


def migrate_file(path: Path, pairs: list[tuple[str, str]], *, dry: bool) -> int:
    text = path.read_text(encoding="utf-8")
    changed = 0
    for old_h, new_h in pairs:
        old_line, new_line = f"## {old_h}", f"## {new_h}"
        old_slug, new_slug = _slug(old_line), _slug(new_line)
        # 1) 标题行：行级精确匹配（避免 "Auto-extracted" 命中
        #    "Auto-extracted preferences"）。
        lines = text.split("\n")
        for i, ln in enumerate(lines):
            if ln.strip() == old_line:
                lines[i] = new_line
                changed += 1
        text = "\n".join(lines)
        # 2) 标记 slug：用 :<slug>:BEGIN / :<slug>:END 全 token 替换，
        #    末尾的 ":" 做边界，"Auto-extracted:" 不会命中
        #    "Auto-extracted-preferences:"。
        for suffix in (":BEGIN", ":END"):
            a = f"XMC-AUTO-EXTRACTED:{old_slug}{suffix}"
            b = f"XMC-AUTO-EXTRACTED:{new_slug}{suffix}"
            if a in text:
                text = text.replace(a, b)
                changed += 1
    if changed and not dry:
        path.write_text(text, encoding="utf-8")
    return changed


def main() -> None:
    dry = "--dry-run" in sys.argv
    profiles = Path.home() / ".xmclaw" / "persona" / "profiles"
    if not profiles.is_dir():
        print(f"未找到 {profiles}")
        return
    total = 0
    for prof in sorted(profiles.iterdir()):
        if not prof.is_dir():
            continue
        for fn, pairs in RENAMES.items():
            f = prof / fn
            if not f.is_file():
                continue
            n = migrate_file(f, pairs, dry=dry)
            if n:
                print(f"{'[dry] ' if dry else ''}{prof.name}/{fn}: {n} 处改动")
                total += n
    print(f"{'（dry-run）' if dry else ''}合计 {total} 处" + ("（未写入）" if dry else "（已写入）"))


if __name__ == "__main__":
    main()
