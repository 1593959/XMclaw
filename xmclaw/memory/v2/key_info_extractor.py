"""KeyInfoExtractor — deterministic regex hook (Phase 3).

THE pain-point fix. User typed: "陪玩店 pw310.wxselling.com，账号
admin / admin888，月流水目标 5 万". Pre-this-module: agent decided
whether to call ``remember`` (which it usually didn't). Post-this-
module: daemon entry hook scans EVERY user message for high-signal
patterns and force-writes via ``MemoryService.remember()`` — agent
is bypassed, the facts are in the store before the LLM even sees
the message.

No LLM call here. Pure regex + heuristic. That's the point: the
guarantee is "if the user mentions a URL / account / password /
numeric goal / explicit memorisation directive, it WILL be stored".

Trigger categories (each maps to a FactKind / FactScope):

  * URLs (http(s)://...)              → project / project
  * Email-like account fragments       → project / project
  * "账号 X / 密码 Y" / "admin/password" patterns → project / project
  * Numeric business goals (月流水 N 万 / target Y) → project / project
  * Explicit "记住 X" / "remember Y" / "下次都" → preference / user
  * "我是 X" / "我叫 X" / "我喜欢 X"  → identity / preference / user
  * "永远别 X" / "不要再 X"           → correction / user

False positives are acceptable — the user can delete from the
Memory Panel UI (Phase 5). False negatives are NOT — that's the
whole point of having a deterministic backstop. Tuning the
patterns is reversible; missing the user's business info is the
silent failure we're trying to eliminate.

Returns ``list[ExtractedKey]`` — call sites turn each into a
``MemoryService.remember(...)`` call.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from xmclaw.memory.v2.models import (
    FactKindStr,
    FactScopeStr,
)


# ── ExtractedKey ──────────────────────────────────────────────────


@dataclass(slots=True)
class ExtractedKey:
    """One auto-detected fact candidate ready for ``remember()``.

    ``confidence`` reflects how strongly the pattern matched. URL
    matches get high confidence (very unambiguous); "我喜欢 X"
    matches get lower (X may be a one-off, not a stable
    preference).
    """

    text: str
    kind: FactKindStr
    scope: FactScopeStr
    confidence: float = 0.85
    pattern_name: str = ""  # for diagnostics / UI hover
    span: tuple[int, int] = (0, 0)  # (start, end) in source message


# ── Patterns ──────────────────────────────────────────────────────

# Each entry: (pattern_name, compiled_regex, builder).
# ``builder`` takes the match + full source, returns ExtractedKey OR
# None (skip). Builders own the text-shaping so capture groups don't
# leak SQL/HTML/whitespace surprises.

_URL_RE = re.compile(
    r"https?://[^\s\"'<>，。、！!?？]+",
    re.IGNORECASE,
)

# Account / password explicit labels — Chinese + English. Matches:
# "账号 admin"、"账号:admin"、"账号=admin"、"账号是 admin"、"账号叫 admin"、
# "username: admin"、"user admin" — the value is captured up to next
# punctuation / whitespace block.
_CRED_RE = re.compile(
    r"""(
        # Chinese: 账号 / 用户名 / 账户 / 帐号 + value
        (?:账号|用户名|账户|帐号)\s*[:：=是叫为]?\s*([A-Za-z0-9._@-]+)
        |
        # 密码 / 口令 + value
        (?:密码|口令)\s*[:：=是叫为]?\s*([^\s,，。、!！?？]+)
        |
        # English: username / user / password / pass
        \b(?:username|user|account)\s*[:=]?\s*([A-Za-z0-9._@-]+)
        |
        \b(?:password|passwd|pwd)\s*[:=]?\s*([^\s,;]+)
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# Shorthand: "admin / admin888" or "admin/admin888" — assume the
# user is giving us a credential pair. Two values separated by /,
# both look like account-shape strings.
_CRED_PAIR_RE = re.compile(
    r"\b([A-Za-z][A-Za-z0-9._-]{1,32})\s*/\s*([A-Za-z][A-Za-z0-9._-]{2,64})\b"
)

# Numeric goals: "月流水 5 万" / "目标 100 单" / "target 50K" /
# "GMV 200 万" — capture goal noun + number + unit.
_GOAL_RE = re.compile(
    r"""(
        # Chinese: GOAL_NOUN [破]? NUM [万千百] [单/件/元/块]?
        (?:目标|月流水|日流水|GMV|流水|营收|订单数?|月活|DAU|MAU|留存率)\s*
        (?:破|做到|做|超过|达到|超)?\s*
        \d+(?:\.\d+)?\s*(?:万|千|百|亿|k|K)?\s*
        (?:单|笔|件|元|块|人|%|百分点)?
        |
        # English: target/goal NUMBER UNIT
        \b(?:target|goal|aim(?:ing)?(?:\s+for)?)\s+
        [\$￥¥]?\d+(?:\.\d+)?[KkMm万千百]?
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# Explicit user memorisation directive: 记住 / 记一下 / 留个底 / 以后都 /
# 下次都 / never / always / from now on
_REMEMBER_DIRECT_RE = re.compile(
    r"""(
        (?:记住|记一下|留个底|帮我记|以后都|下次都|永远都?|今后)
        \s*[:：,，]?\s*(.{2,200}?)
        (?:$|[。！？!?\n])
        |
        \b(?:from\s+now\s+on|always|never|going\s+forward)\s*[,:]?\s*
        (.{2,200}?)(?:$|[.!?\n])
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# Self-identity: "我是 X" / "我叫 X" / "I'm X"
_IDENTITY_RE = re.compile(
    r"""(
        我(?:是|叫|名字是|名叫)\s*([^\s,，。、!！?？]{1,40})
        |
        \b(?:I(?:'m| am)|my\s+name\s+is)\s+([A-Z][A-Za-z一-鿿]{1,40})
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# Preference: 我喜欢 X / I prefer X / I like X
_PREFERENCE_RE = re.compile(
    r"""(
        我(?:喜欢|偏好|习惯用?|爱用|爱)\s*([^\s,，。、!！?？]{1,40})
        |
        \bI\s+(?:like|prefer|love|use)\s+([A-Za-z][A-Za-z0-9 ]{1,40})
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# Correction: 不要 / 别 / 错了 / don't / stop
_CORRECTION_RE = re.compile(
    r"""(
        (?:不要再?|别再?|错了|不对|不是)\s*([^,，。、!！?？\n]{2,80})
        |
        \b(?:don'?t|stop|never)\s+([a-z][a-z0-9 ]{2,60})
    )""",
    re.IGNORECASE | re.VERBOSE,
)


# ── Extractor ────────────────────────────────────────────────────


def extract_keys(message: str) -> list[ExtractedKey]:
    """Scan a user message for high-signal facts.

    Returns a list of ExtractedKey candidates. Empty list when
    nothing matched. Order is roughly the order patterns are
    listed (URL → credential → goal → directive → identity →
    preference → correction).

    Caller is responsible for de-duping at the MemoryService layer
    (remember() is idempotent on text, so duplicate ExtractedKey
    with same text just bumps evidence_count).
    """
    if not message or not message.strip():
        return []

    out: list[ExtractedKey] = []
    seen_spans: list[tuple[int, int]] = []

    def _add(
        text: str,
        *,
        kind: FactKindStr,
        scope: FactScopeStr,
        confidence: float,
        pattern_name: str,
        span: tuple[int, int],
    ) -> None:
        text = text.strip()
        if len(text) < 2 or len(text) > 500:
            return
        # Skip if this span fully overlaps a previously-recorded span
        # (avoid duplicate facts from nested patterns).
        for s0, s1 in seen_spans:
            if span[0] >= s0 and span[1] <= s1:
                return
        seen_spans.append(span)
        out.append(ExtractedKey(
            text=text, kind=kind, scope=scope,
            confidence=confidence, pattern_name=pattern_name,
            span=span,
        ))

    # ── URL ──
    for m in _URL_RE.finditer(message):
        url = m.group(0).rstrip(".,;)")
        _add(
            f"网址: {url}",
            kind="project", scope="project",
            confidence=0.95, pattern_name="url",
            span=m.span(),
        )

    # ── Credentials ──
    for m in _CRED_RE.finditer(message):
        # Pick the non-empty group.
        value = next((g for g in m.groups()[1:] if g), None)
        if value is None:
            continue
        label = m.group(0).strip()
        _add(
            f"凭据: {label}",
            kind="project", scope="project",
            confidence=0.90, pattern_name="credential",
            span=m.span(),
        )

    # ── admin/admin888-style pair ──
    for m in _CRED_PAIR_RE.finditer(message):
        # Only treat as credential pair if it doesn't look like a path
        # / fraction / URL fragment / dimensional unit.
        a, b = m.group(1), m.group(2)
        if a.lower() == "http" or a.lower() == "https":
            continue
        # Heuristic: at least one of the two must contain a digit OR
        # be a known cred word. Otherwise looks more like "MIT/Apache"
        # license-style — skip.
        if not (any(ch.isdigit() for ch in a + b)):
            common = {"admin", "root", "user", "test", "demo"}
            if a.lower() not in common and b.lower() not in common:
                continue
        _add(
            f"账号密码对: {a} / {b}",
            kind="project", scope="project",
            confidence=0.85, pattern_name="cred_pair",
            span=m.span(),
        )

    # ── Numeric goals ──
    for m in _GOAL_RE.finditer(message):
        _add(
            f"业务目标: {m.group(0).strip()}",
            kind="project", scope="project",
            confidence=0.85, pattern_name="goal",
            span=m.span(),
        )

    # ── Explicit "remember X" ──
    for m in _REMEMBER_DIRECT_RE.finditer(message):
        # Get the captured payload.
        payload = next((g for g in m.groups()[1:] if g), None)
        if payload is None:
            continue
        _add(
            payload.strip().rstrip(".,;:。！？"),
            kind="preference", scope="user",
            confidence=0.95, pattern_name="remember_directive",
            span=m.span(),
        )

    # ── Identity ──
    for m in _IDENTITY_RE.finditer(message):
        value = next((g for g in m.groups()[1:] if g), None)
        if value is None:
            continue
        _add(
            f"身份: {m.group(0).strip()}",
            kind="identity", scope="user",
            confidence=0.85, pattern_name="identity",
            span=m.span(),
        )

    # ── Preference ──
    for m in _PREFERENCE_RE.finditer(message):
        value = next((g for g in m.groups()[1:] if g), None)
        if value is None:
            continue
        _add(
            f"偏好: {m.group(0).strip()}",
            kind="preference", scope="user",
            confidence=0.80, pattern_name="preference",
            span=m.span(),
        )

    # ── Correction ──
    for m in _CORRECTION_RE.finditer(message):
        value = next((g for g in m.groups()[1:] if g), None)
        if value is None:
            continue
        _add(
            f"纠正: {m.group(0).strip()}",
            kind="correction", scope="user",
            confidence=0.90, pattern_name="correction",
            span=m.span(),
        )

    return out


# ── Helper: bulk write to MemoryService ───────────────────────────


async def extract_and_remember(
    message: str,
    memory_service: Any,
    *,
    source_event_id: str | None = None,
) -> list[Any]:
    """Convenience wrapper: extract_keys → MemoryService.remember.

    Returns the list of Fact objects written (one per extracted key).
    Errors during individual writes are logged but don't abort the
    batch — partial success is fine, the next user message will
    retry the missed ones via idempotent upsert.
    """
    from xmclaw.utils.log import get_logger
    log = get_logger(__name__)
    keys = extract_keys(message)
    if not keys:
        return []
    written = []
    for key in keys:
        try:
            fact = await memory_service.remember(
                key.text,
                kind=key.kind,
                scope=key.scope,
                confidence=key.confidence,
                source_event_id=source_event_id,
            )
            written.append(fact)
        except Exception as exc:  # noqa: BLE001 — never fail user turn
            log.warning(
                "key_info_extractor.remember_failed pattern=%s err=%s",
                key.pattern_name, exc,
            )
    return written


__all__ = [
    "ExtractedKey",
    "extract_and_remember",
    "extract_keys",
]
