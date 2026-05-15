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

# ── Wave 27 Phase 3.1 — additional coverage ────────────────────
# User asked for "全场景" coverage. Beyond the 8 original patterns:

# Email — bare local@domain.
_EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
)

# Phone numbers — accept 11-digit Mainland mobiles (1[3-9]xxxxxxxx),
# +-country-prefixed international (+86 / +1 / etc), and 800/400
# vanity numbers. Hyphens / spaces tolerated.
_PHONE_RE = re.compile(
    r"""(
        \+?\d{1,3}[\s-]?\d{3,4}[\s-]?\d{3,4}[\s-]?\d{3,5}
        |
        \b1[3-9]\d{9}\b                # 11-digit CN mobile
        |
        \b(?:400|800)[\s-]?\d{3,4}[\s-]?\d{3,4}\b
    )""",
    re.VERBOSE,
)

# Social / IM handles — WeChat / QQ / Telegram / Github / Twitter
# patterns. Capture labels + value when present; bare @handle for
# Twitter; "微信号 X" / "QQ X" with optional separator.
_SOCIAL_RE = re.compile(
    r"""(
        # 微信号 / 微信 X — allow a Chinese filler word like
        # "号/帐号/账号/名字/叫" between label and value
        (?:微信号?|wechat)
        (?:\s*(?:号|账号|帐号|名字|叫|是)?)?\s*[:：=]?\s*
        ([A-Za-z][A-Za-z0-9_-]{4,30})
        |
        # QQ 号 / QQ X — pure digits, 5-13 long
        \bQQ
        (?:\s*(?:号|账号|帐号))?\s*[:：=]?\s*
        (\d{5,13})\b
        |
        # Telegram / TG handle
        (?:telegram|tg)
        (?:\s*(?:号|账号|名字))?\s*[:：=]?\s*@?
        ([A-Za-z][A-Za-z0-9_]{4,30})
        |
        # GitHub user — allow a Chinese filler word like "仓库/账号/
        # repo" between "github" and the value. acme-team/foo style
        # captures "acme-team/foo" up to whitespace.
        (?:github|gh)
        (?:\s*(?:仓库|repo|账号|帐号|user|用户))?\s*[:：=]?\s*
        ([A-Za-z0-9][A-Za-z0-9/_-]{1,60})
        |
        # @username — only treat as social when it looks like a handle
        # and isn't an email (no "@x@y" or "@..."). Conservative:
        # require it to be standalone (word boundary on both sides).
        (?<![A-Za-z0-9.])@([A-Za-z][A-Za-z0-9_]{2,30})\b
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# Local file paths (Windows + POSIX). Capture absolute / drive paths
# OR ~/relative-from-home shorthand. Generous on the body but require
# a path-shaped start to avoid grabbing sentences.
_PATH_RE = re.compile(
    r"""(
        # Windows: C:\Users\... / D:/foo / \\server\share
        \b[A-Za-z]:[\\/](?:[^\s"'<>|*?:\n]+[\\/]?)+[^\s"'<>|*?:\n]+
        |
        \\\\[A-Za-z0-9_.-]+[\\/][^\s"'<>|*?:\n]+
        |
        # POSIX absolute: /etc/hosts / /usr/local/bin
        (?:^|\s)(/(?:home|etc|var|usr|opt|root|tmp|mnt|srv|Users)/[^\s"'<>|*?:\n]+)
        |
        # ~/relative
        (?:^|\s)(~/[^\s"'<>|*?:\n]+)
    )""",
    re.VERBOSE | re.MULTILINE,
)

# Tech stack — "用 X" / "栈是 ..." / explicit language/framework mention.
# Conservative: only match common stacks we want as facts.
_STACK_RE = re.compile(
    r"""(
        # 我?用 X 写/做/开发
        我?用\s*([A-Z][A-Za-z0-9+#./_-]{1,30})\s*(?:写|做|开发|搭|跑|部署)?
        |
        # 栈 / tech stack is X
        (?:栈是|技术栈是?|stack(?:\s+is)?)\s*[:：]?\s*([A-Za-z][A-Za-z0-9+#./, _-]{2,80})
        |
        # X 版本 N.N — version statement
        \b([A-Z][A-Za-z+#.-]{1,20})\s*(?:版本是?|version\s*(?:is|=)?)\s*([0-9]+(?:\.[0-9]+)*)
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# Schedule / deadline — 截止 / deadline / 月底前 / Q3 前 / by Friday.
_DEADLINE_RE = re.compile(
    r"""(
        # 截止 / deadline
        (?:截止(?:日期)?|ddl|deadline)\s*[:：是为]?\s*([^,，。、!！?？\n]{2,60})
        |
        # 月底 / 周末 / Q3 末 / 双11 / 春节 前
        (?:今年|今|本)?(?:月底|周末|Q[1-4]\s*(?:末|前|内)?|双11|春节|国庆|618|11月|12月)\s*(?:前|内|之前)?
        |
        # by Friday / before March
        \bby\s+(?:next\s+)?(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# Date / time mention — explicit dates (2026-05-15) or natural-language
# Chinese dates (明天 / 后天 / 下周一 / 5月15日 / 每周三 10点).
_DATETIME_RE = re.compile(
    r"""(
        # ISO-style 2026-05-15 / 2026/5/15 / 2026.5.15
        \b20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}\b
        |
        # 中文日期 5月15日 / 5月15号
        \d{1,2}\s*月\s*\d{1,2}\s*[日号]
        |
        # 相对日期
        (?:今天|明天|后天|昨天|大?前天|大?后天|下下?周[一二三四五六日天]?|上周[一二三四五六日天]?|每周[一二三四五六日天]?|每天|每月|每年)
        |
        # 时间点 10点 / 上午 9 点半
        (?:上午|下午|早上|晚上|凌晨|中午)?\s*\d{1,2}\s*(?:点|时)(?:\s*\d{1,2}\s*分|半|整)?
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# Budget / money — 预算 N 元 / 预算 N 万 / $N / ￥N
_MONEY_RE = re.compile(
    r"""(
        # 预算 / cost / 花了 / 报价 + amount
        (?:预算|费用|成本|报价|售价|定价|花了|花费|cost|budget|price)\s*(?:为|是|约)?\s*
        [\$￥¥]?\s*\d+(?:\.\d+)?\s*(?:万|千|百|亿|k|K|m|M)?\s*(?:元|块|RMB|人民币|美金|USD|刀|CNY)?
        |
        # Standalone currency amount $100 / ￥1000
        [\$￥¥]\s*\d+(?:\.\d+)?(?:[KkMm万千百])?
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# Relationship / family / colleague: 我朋友 X / 我老婆 X / 我老板 X /
# 我同事 X — captures (rel_word, name).
_RELATIONSHIP_RE = re.compile(
    r"""(
        我(?:朋友|男朋友|女朋友|男友|女友|老公|老婆|男人|女人|对象|爱人|
            老板|领导|上司|同事|队友|合伙人|室友|同学|老师|学生|师傅|徒弟|
            爸|妈|爸爸|妈妈|父亲|母亲|爷爷|奶奶|外公|外婆|哥|姐|弟|妹|
            儿子|女儿|孩子|宝宝)
        \s*(?:叫|是|的名字是)?\s*([^\s,，。、!！?？]{1,30})
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# Hard constraints — 必须 / 永远 / 一定 / always / must / never (stronger
# than `correction`, also fires on positive obligations).
_CONSTRAINT_RE = re.compile(
    r"""(
        # 必须 / 一定要 / 一律 / 永远 + clause
        (?:必须|一定(?:要)?|一律|永远(?:都)?|绝对(?:不可?)?|从来不?|从不|从未)\s*
        ([^,，。、!！?？\n]{2,80})
        |
        # English: must / always / never X
        \b(?:must|always|never|under no circumstances)\s+
        ([a-z][a-z0-9 -]{2,60})
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# Organization / product — "公司是 X" / "产品名 X" / "项目 X".
_ORG_RE = re.compile(
    r"""(
        (?:公司|项目|产品|品牌|店铺|工作室|平台)
        (?:名字?|叫|是)?\s*[:：]?\s*
        ([A-Za-z一-鿿][A-Za-z0-9一-鿿._-]{1,40})
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

    # ── Hard constraints (FIRST — beats remember_directive on same
    # span since constraint preserves the negation prefix ("永远别 X"
    # → correction, not "在 X" preference which inverts meaning) ──
    for m in _CONSTRAINT_RE.finditer(message):
        _add(
            f"约束: {m.group(0).strip()}",
            kind="correction", scope="user",
            confidence=0.92, pattern_name="constraint",
            span=m.span(),
        )

    # ── Explicit "remember X" — only matches AFTER constraint has had
    # a chance, so "永远别 X" stays as constraint (correction kind). ──
    for m in _REMEMBER_DIRECT_RE.finditer(message):
        # Get the captured payload.
        payload = next((g for g in m.groups()[1:] if g), None)
        if payload is None:
            continue
        # Preserve the trigger word in the stored text so semantics
        # don't invert (e.g. "永远别 X" must keep "永远别" to mean
        # NEGATION, not "下次都 X" which means INSTRUCTION).
        full = m.group(0).strip().rstrip(".,;:。！？")
        _add(
            full,
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

    # ── Wave 27 Phase 3.1 — extended coverage ──

    # ── Email (project scope — likely a contact for the project) ──
    for m in _EMAIL_RE.finditer(message):
        _add(
            f"邮箱: {m.group(0).strip()}",
            kind="project", scope="project",
            confidence=0.92, pattern_name="email",
            span=m.span(),
        )

    # ── Phone numbers ──
    for m in _PHONE_RE.finditer(message):
        num = m.group(0).strip()
        # Skip ambiguous short matches that look like price/ID rather
        # than a phone — require ≥ 7 effective digits.
        digits = "".join(ch for ch in num if ch.isdigit())
        if len(digits) < 7:
            continue
        _add(
            f"电话: {num}",
            kind="project", scope="project",
            confidence=0.85, pattern_name="phone",
            span=m.span(),
        )

    # ── Social handles ──
    for m in _SOCIAL_RE.finditer(message):
        handle = next((g for g in m.groups()[1:] if g), None)
        if handle is None:
            continue
        _add(
            f"社交账号: {m.group(0).strip()}",
            kind="project", scope="project",
            confidence=0.85, pattern_name="social",
            span=m.span(),
        )

    # ── File paths ──
    for m in _PATH_RE.finditer(message):
        path = m.group(0).strip()
        # Drop obviously trailing punctuation captured by the wide regex.
        path = path.rstrip(".,;)")
        # Skip excessively short / shouty captures.
        if len(path) < 4:
            continue
        _add(
            f"路径: {path}",
            kind="project", scope="project",
            confidence=0.82, pattern_name="path",
            span=m.span(),
        )

    # ── Tech stack ──
    for m in _STACK_RE.finditer(message):
        _add(
            f"技术: {m.group(0).strip()}",
            kind="project", scope="project",
            confidence=0.78, pattern_name="stack",
            span=m.span(),
        )

    # ── Deadlines ──
    for m in _DEADLINE_RE.finditer(message):
        _add(
            f"截止: {m.group(0).strip()}",
            kind="commitment", scope="project",
            confidence=0.82, pattern_name="deadline",
            span=m.span(),
        )

    # ── Datetime mentions ──
    for m in _DATETIME_RE.finditer(message):
        # Don't add bare "今天" / "明天" — too noisy as standalone facts.
        text_norm = m.group(0).strip()
        if text_norm in ("今天", "明天", "昨天"):
            continue
        _add(
            f"时间: {text_norm}",
            kind="commitment", scope="project",
            confidence=0.70, pattern_name="datetime",
            span=m.span(),
        )

    # ── Money / budget ──
    for m in _MONEY_RE.finditer(message):
        _add(
            f"金额: {m.group(0).strip()}",
            kind="project", scope="project",
            confidence=0.85, pattern_name="money",
            span=m.span(),
        )

    # ── Relationships ──
    for m in _RELATIONSHIP_RE.finditer(message):
        _add(
            f"关系: {m.group(0).strip()}",
            kind="identity", scope="user",
            confidence=0.88, pattern_name="relationship",
            span=m.span(),
        )

    # ── Org / product / project name ──
    for m in _ORG_RE.finditer(message):
        _add(
            f"组织: {m.group(0).strip()}",
            kind="project", scope="project",
            confidence=0.78, pattern_name="org",
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

    Wave-27 fix-9: facts extracted from the SAME user message are
    auto-linked via SAME_TOPIC edges. Co-occurrence in a single
    user input is a STRUCTURAL signal that the regex layer was
    discarding. The user typed
    "https://pw310.wxselling.com 账号 admin 密码 admin888" — the
    URL, the account, and the password are obviously about ONE
    thing, but the per-fact vector-based SAME_TOPIC scan only
    fires for same-kind vector-close pairs ("账号 admin" and
    "密码 admin888" cluster; the URL text embeds far enough away
    to miss the threshold). Cross-fact pairwise edges fix the
    graph view ("why isn't the URL linked to the credentials?").
    Symmetric edges so the graph walks both directions.
    """
    from xmclaw.utils.log import get_logger
    from xmclaw.memory.v2.models import RelationKind
    log = get_logger(__name__)
    keys = extract_keys(message)
    if not keys:
        return []
    written = []
    for key in keys:
        try:
            # Wave-27 fix-12: bucket inference from (kind, scope) so
            # the persona renderer can route each fact to the right
            # MD file. See ``llm_extractor.llm_extract_and_remember``
            # for the same mapping — kept consistent across the two
            # extractor entry points.
            bucket = ""
            if key.kind == "identity":
                if key.scope == "session":
                    bucket = "agent_identity"
                elif key.scope == "user":
                    bucket = "user_identity"
            elif key.kind == "preference" and key.scope == "user":
                bucket = "user_preference"
            fact = await memory_service.remember(
                key.text,
                kind=key.kind,
                scope=key.scope,
                confidence=key.confidence,
                source_event_id=source_event_id,
                bucket=bucket,
            )
            written.append(fact)
        except Exception as exc:  # noqa: BLE001 — never fail user turn
            log.warning(
                "key_info_extractor.remember_failed pattern=%s err=%s",
                key.pattern_name, exc,
            )

    # Wave-27 fix-9: pairwise SAME_TOPIC edges across co-extracted
    # facts. Skip when only one fact came out (no pairs to link).
    if len(written) >= 2:
        unique_ids = list({f.id: f for f in written}.values())
        for i, fact_a in enumerate(unique_ids):
            for fact_b in unique_ids[i + 1:]:
                if fact_a.id == fact_b.id:
                    continue
                for src, dst in (
                    (fact_a.id, fact_b.id),
                    (fact_b.id, fact_a.id),
                ):
                    try:
                        await memory_service.relate(
                            source_fact_id=src,
                            target_fact_id=dst,
                            kind=RelationKind.SAME_TOPIC,
                            strength=0.80,
                            auto_extracted=True,
                        )
                    except Exception as exc:  # noqa: BLE001
                        log.warning(
                            "key_info_extractor.cooccur_link_failed "
                            "src=%s dst=%s err=%s",
                            src[:32], dst[:32], exc,
                        )

    return written


__all__ = [
    "ExtractedKey",
    "extract_and_remember",
    "extract_keys",
]
