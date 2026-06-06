"""清理记忆里的"垃圾污染"——主要是被误存成长期事实的**一次性命令**。

最典型：用户当下说"希望删除所有无法正常使用的技能"被旧 regex 误判成人生目标，
存成 ``目标: 删除所有无法正常使用的技能``（2026-06-07 已在写入端修复，但存量还在）。

用法（daemon 需在跑）：
    python scripts/cleanup_memory_pollution.py            # 干跑：只列要删的 + 全库概览
    python scripts/cleanup_memory_pollution.py --apply    # 真删

默认**干跑**，不删任何东西。先看清楚再 --apply。
"""
from __future__ import annotations

import asyncio
import sys
from collections import Counter
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from xmclaw.memory.v2.key_info_extractor import _is_transient_command  # noqa: E402

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

TOKEN = (Path.home() / ".xmclaw" / "v2" / "pairing_token.txt").read_text(
    encoding="utf-8",
).strip()
BASE = "http://127.0.0.1:8766/api/v2/memory/v2"

# "目标:" / "偏好:" 等前缀（regex 提取器加的），剥掉后判断是不是一次性命令。
_PREFIXES = ("目标:", "目标：", "偏好:", "偏好：", "纠正:", "纠正：")


def _strip_prefix(text: str) -> str:
    t = (text or "").strip()
    for p in _PREFIXES:
        if t.startswith(p):
            return t[len(p):].strip()
    return t


def _is_garbage(fact: dict) -> str | None:
    """返回污染原因字符串，或 None（不是垃圾）。"""
    text = (fact.get("text") or "").strip()
    # ① 一次性命令被存成目标/偏好/纠正
    body = _strip_prefix(text)
    if body != text and _is_transient_command(body):
        return "一次性命令误存为长期事实"
    # ② 纯命令文本（无前缀也命中祈使动词，且很短）
    if len(text) <= 40 and _is_transient_command(text):
        return "疑似一次性命令"
    return None


async def list_all(c: httpx.AsyncClient) -> list[dict]:
    out: list[dict] = []
    offset = 0
    while True:
        r = await c.get(f"{BASE}/facts",
                        params={"token": TOKEN, "limit": 500, "offset": offset})
        if r.status_code != 200:
            break
        facts = (r.json() or {}).get("facts") or []
        if not facts:
            break
        out.extend(facts)
        if len(facts) < 500:
            break
        offset += len(facts)
    return out


async def main() -> None:
    apply = "--apply" in sys.argv
    async with httpx.AsyncClient(timeout=30.0) as c:
        facts = await list_all(c)
        print(f"全库共 {len(facts)} 条记忆。\n")

        # ── 透明概览：按 bucket / kind 统计 ──
        by_bucket = Counter((f.get("bucket") or "?") for f in facts)
        by_kind = Counter((f.get("kind") or "?") for f in facts)
        print("【按 bucket】")
        for b, n in by_bucket.most_common():
            print(f"  {n:4d}  {b}")
        print("\n【按 kind】")
        for k, n in by_kind.most_common():
            print(f"  {n:4d}  {k}")

        # ── 标记垃圾 ──
        garbage = [(f, r) for f in facts if (r := _is_garbage(f))]
        print(f"\n标记为污染：{len(garbage)} 条")
        for f, reason in garbage:
            print(f"  [{reason}] {(f.get('text') or '')[:60]}  (id={f.get('id','')[:10]})")

        if not garbage:
            print("\n没发现这类污染。")
            return
        if not apply:
            print(f"\n（干跑，未删除）确认无误后加 --apply 真删这 {len(garbage)} 条。")
            return

        deleted = 0
        for f, _ in garbage:
            try:
                r = await c.delete(f"{BASE}/facts/{f['id']}", params={"token": TOKEN})
                if r.status_code == 200:
                    deleted += 1
            except Exception as exc:  # noqa: BLE001
                print(f"  删除失败 {f.get('id','')[:10]}: {exc}")
        print(f"\n已删除 {deleted}/{len(garbage)} 条污染。")


if __name__ == "__main__":
    asyncio.run(main())
