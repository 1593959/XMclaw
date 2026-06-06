#!/usr/bin/env python3
"""XMclaw 技能系统整理脚本。

策略：
1. auto-* 技能（90个）：按功能域分组，每组保留 confidence 最高的 1-2 个，删除其余
2. 社区技能（335个）：检查格式合规性，标记损坏的
3. 生成整理报告
"""

import shutil
import re
from pathlib import Path
from collections import defaultdict
from dataclasses import dataclass


@dataclass
class AutoSkill:
    name: str
    path: Path
    confidence: float
    category: str
    description: str
    body: str


def categorize(name: str) -> str:
    """按命名规则分类 auto-* 技能。"""
    n = name[5:]  # 去掉 auto-
    if any(x in n for x in ['wechat', 'gui', 'window', 'screen', 'focus', 'click', 'ocr']):
        return 'gui'
    if any(x in n for x in ['scrape', 'web', 'page', 'visual', 'browser', 'navigate', 'webhook']):
        return 'web'
    if any(x in n for x in ['memory', 'dedup', 'compact', 'inspect', 'audit', 'forget', 'curate', 'remember']):
        return 'memory'
    if any(x in n for x in ['think', 'reason', 'reflect', 'iterate']):
        return 'cognition'
    if any(x in n for x in ['orchestrate', 'workflow', 'automate']):
        return 'orchestration'
    if any(x in n for x in ['verify', 'validate', 'check', 'debug']):
        return 'verification'
    return 'other'


def parse_auto_skill(skill_dir: Path) -> AutoSkill | None:
    """解析一个 auto-* 技能目录。"""
    skill_md = skill_dir / 'SKILL.md'
    if not skill_md.exists():
        return None

    body = skill_md.read_text(encoding='utf-8', errors='replace')

    conf_match = re.search(r'confidence:\s*([0-9.]+)', body)
    confidence = float(conf_match.group(1)) if conf_match else 0.0

    desc_match = re.search(r'description:\s*(.+)', body)
    description = desc_match.group(1).strip() if desc_match else ''

    return AutoSkill(
        name=skill_dir.name,
        path=skill_dir,
        confidence=confidence,
        category=categorize(skill_dir.name),
        description=description,
        body=body,
    )


def main():
    auto_dir = Path.home() / '.xmclaw/skills_user'
    autos = [parse_auto_skill(d) for d in auto_dir.iterdir() if d.is_dir() and d.name.startswith('auto-')]
    autos = [a for a in autos if a is not None]

    # 按类别分组
    by_cat = defaultdict(list)
    for a in autos:
        by_cat[a.category].append(a)

    # 整理策略：每组保留 confidence 最高的 1-2 个
    KEEP_TOP_N = {
        'gui': 4,
        'web': 4,
        'memory': 5,
        'cognition': 2,
        'orchestration': 1,
        'verification': 1,
        'other': 1,
    }

    to_delete: list[AutoSkill] = []
    to_keep: list[AutoSkill] = []

    print("=" * 60)
    print("XMclaw 技能系统整理报告")
    print("=" * 60)
    print()

    for cat, items in sorted(by_cat.items(), key=lambda x: -len(x[1])):
        items.sort(key=lambda x: -x.confidence)
        keep_n = KEEP_TOP_N.get(cat, 1)
        keep = items[:keep_n]
        delete = items[keep_n:]

        print(f"[{cat}] total={len(items)} keep={len(keep)} delete={len(delete)}")
        for k in keep:
            print(f"  [KEEP] {k.name} (confidence={k.confidence})")
        for d in delete:
            print(f"  [DEL]  {d.name} (confidence={d.confidence})")
        print()

        to_keep.extend(keep)
        to_delete.extend(delete)

    print("-" * 60)
    print(f"总计: {len(autos)} 个 auto-* 技能")
    print(f"保留: {len(to_keep)} 个")
    print(f"删除: {len(to_delete)} 个")
    print("-" * 60)
    print()

    # 执行删除
    deleted_count = 0
    for skill in to_delete:
        try:
            shutil.rmtree(skill.path)
            print(f"已删除: {skill.name}")
            deleted_count += 1
        except Exception as e:
            print(f"删除失败 {skill.name}: {e}")

    print()
    print(f"实际删除: {deleted_count} 个技能")
    print(f"剩余 auto-* 技能: {len(to_keep)} 个")

    # 检查社区技能格式
    agents_dir = Path.home() / '.agents/skills'
    if agents_dir.exists():
        bad_community = []
        for d in agents_dir.iterdir():
            if not d.is_dir():
                continue
            skill_md = d / 'SKILL.md'
            if not skill_md.exists():
                bad_community.append((d.name, "缺少 SKILL.md"))
                continue
            body = skill_md.read_text(encoding='utf-8', errors='replace')
            if not body.strip().startswith('---'):
                bad_community.append((d.name, "SKILL.md 缺少 frontmatter"))

        print()
        print("=" * 60)
        print(f"社区技能检查: {len(list(agents_dir.iterdir()))} 个目录")
        print(f"格式问题: {len(bad_community)} 个")
        if bad_community:
            for name, reason in bad_community[:10]:
                print(f"  ⚠️  {name}: {reason}")
            if len(bad_community) > 10:
                print(f"  ... 还有 {len(bad_community) - 10} 个")
        print("=" * 60)


if __name__ == '__main__':
    main()
