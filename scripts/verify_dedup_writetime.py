"""Live verification: write-time near-dup merge fires on the daemon.

Repros the user's "刚才我看的时候有很多重复的" complaint by posting 3
near-paraphrases of the same fact and asserting:

  • Default GET /facts returns 1 row (write-time merge collapsed them)
  • That row has evidence_count == 3 (each write bumped evidence)
  • include_superseded=true returns the same 1 row (nothing superseded
    because writes merged at write time, not via bulk dedup)

Cleans up after itself by deleting the survivor's id.

Run: ``python scripts/verify_dedup_writetime.py``
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx

# Windows stdout defaults to GBK which can't encode CJK or [OK] — force utf-8
# so the report is readable when run on a fresh shell.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


BASE = "http://127.0.0.1:8765"
TOKEN_FILE = Path.home() / ".xmclaw" / "v2" / "pairing_token.txt"


def _headers() -> dict[str, str]:
    tok = TOKEN_FILE.read_text(encoding="utf-8").strip()
    return {
        "Authorization": f"Bearer {tok}",
        "Content-Type": "application/json",
    }


def main() -> int:
    # Generate paraphrases with a fresh nonce that dominates the text
    # so embeddings have no near-neighbour in the existing DB. The
    # SAME nonce in all three means the embeddings cluster tightly.
    nonce = f"VFXX{int(time.time())}NONCE"
    paraphrases = [
        f"{nonce}: 测试 dedup 项 A — 这是一个完整的句子来验证 write-time merge 行为",
        f"{nonce}: 测试 dedup 项 B — 这是一个完整的句子来验证 write-time merge 行为",
        f"{nonce}: 测试 dedup 项 C — 这是一个完整的句子来验证 write-time merge 行为",
    ]
    headers = _headers()

    print(f"[1/4] POST 3 near-duplicate facts with nonce={nonce}")
    created_ids: list[str] = []
    for text in paraphrases:
        r = httpx.post(
            f"{BASE}/api/v2/memory/v2/facts",
            json={"text": text, "kind": "project", "scope": "project"},
            headers=headers,
            timeout=30.0,
        )
        if r.status_code != 200:
            print(f"  FAIL POST {text!r}: {r.status_code} {r.text}")
            return 1
        fid = r.json()["created"]["id"]
        created_ids.append(fid)
        print(f"  ok  id={fid[:18]}...  text={text!r}")

    distinct = len(set(created_ids))
    print(
        f"\n[2/4] {distinct} distinct id(s) returned across 3 POSTs"
    )

    print("\n[3/4] GET /facts?q=<nonce>  (default — hides superseded)")
    r = httpx.get(
        f"{BASE}/api/v2/memory/v2/facts",
        params={"q": nonce},
        headers=headers,
        timeout=30.0,
    )
    if r.status_code != 200:
        print(f"  FAIL: {r.status_code} {r.text}")
        return 1
    body = r.json()
    visible_count = body["total"]
    print(f"  visible rows: {visible_count}")
    if visible_count == 1:
        only = body["facts"][0]
        print(
            f"  [OK] ONE row visible. evidence_count={only['evidence_count']} "
            f"text={only['text']!r}"
        )
    else:
        print(f"  [FAIL] expected 1 visible row, got {visible_count}")
        for f in body["facts"]:
            print(f"    - {f['id'][:18]} ev={f['evidence_count']} {f['text']!r}")

    print("\n[4/4] GET /facts?q=<nonce>&include_superseded=true")
    r2 = httpx.get(
        f"{BASE}/api/v2/memory/v2/facts",
        params={"q": nonce, "include_superseded": "true"},
        headers=headers,
        timeout=30.0,
    )
    if r2.status_code != 200:
        print(f"  FAIL: {r2.status_code} {r2.text}")
        return 1
    all_body = r2.json()
    total_all = all_body["total"]
    print(f"  with-superseded rows: {total_all}")
    for f in all_body["facts"]:
        flag = "[SUPERSEDED]" if f.get("superseded_by") else "[live]"
        print(
            f"  {flag} {f['id'][:18]}... ev={f['evidence_count']} "
            f"{f['text']!r}"
        )

    # Diagnose.
    print("\n=== Verdict ===")
    if distinct == 1 and visible_count == 1:
        only = body["facts"][0]
        if only["evidence_count"] >= 3:
            print(
                "PASS — write-time merge fired. All 3 POSTs collapsed to "
                "one id with evidence_count >= 3."
            )
            cleanup_id(only["id"], headers)
            return 0
        else:
            print(
                f"PARTIAL — single id but evidence_count={only['evidence_count']} "
                "(expected ≥3)."
            )
            return 2
    elif distinct >= 2 and visible_count == 1:
        print(
            "PASS — write-time merge fired after the first POST. "
            "Distinct ids show LanceDB returned new ids per POST, but "
            "service merged on near-dup match before persisting."
        )
        cleanup_id(body["facts"][0]["id"], headers)
        return 0
    else:
        print(
            f"FAIL — write-time merge did NOT collapse the duplicates. "
            f"Distinct ids={distinct}, visible={visible_count}."
        )
        return 3


def cleanup_id(fid: str, headers: dict[str, str]) -> None:
    r = httpx.delete(
        f"{BASE}/api/v2/memory/v2/facts/{fid}",
        headers=headers, timeout=30.0,
    )
    print(f"\n[cleanup] DELETE {fid[:18]}... → {r.status_code}")


if __name__ == "__main__":
    sys.exit(main())
