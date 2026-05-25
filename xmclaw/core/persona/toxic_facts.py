"""Toxic self-capability-denial fact filter.

Background
==========

Recurring user pain (chat-b3c614bc, 2026-05-25; previously partially
patched by 06d3ba3 / 448a3f5):

  1. An LLM turn hallucinates a self-capability limitation it doesn't
     actually have ("我看不到聊天里的图片", "I can't see pasted
     images", "视觉能力受限").
  2. The percept extractor / fact extractor captures this as a
     high-confidence "fact about the agent/user".
  3. The fact is written to v2 LanceDB AND rendered into the persona
     markdown files (``USER.md`` / ``IDENTITY.md``).
  4. Every subsequent turn loads the persona file into the system
     prompt as ground truth. The model dutifully obeys the
     hallucinated limitation → repeats "我看不到", reinforcing the
     fact again on the next extractor pass.
  5. The agent ends up confidently denying capabilities it has,
     forever, until someone manually edits the MD.

Prior fixes attacked the symptom (LanceDB facts via 06d3ba3, message-
boundary ground-truth note via 06d3ba3) but missed the persona
markdown file path. This module is the structural fix:

* ``is_toxic_self_capability_denial(text)`` returns True for any
  fact text that asserts the agent CAN'T do something it can.
* Applied at TWO chokepoints:

    - Write time: ``_write_facts_to_memory`` filters before the
      v2.remember() call.
    - Read time: ``v2_renderer._render_section_body`` filters
      before formatting bullets. This catches already-poisoned
      LanceDB rows automatically — no manual cleanup needed.

Conservative pattern list — false positives are CHEAP (we drop one
fact bullet) but false negatives are EXPENSIVE (the lie persists for
weeks). Add patterns when new poisoning forms surface; don't try to
be clever with LLM-classifying.
"""
from __future__ import annotations

import re
from typing import Final

# Patterns matched against the fact's text (case-insensitive,
# regex-anchored loosely so "I can't see" matches inside longer
# bullets). Order doesn't matter — first match wins.
#
# Each entry is a (compiled regex, human-readable id) tuple. The id
# is logged when a fact is rejected so ops can see WHICH pattern
# fired (helps debug false positives).
_PATTERNS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    # Chinese self-capability denial.
    (re.compile(r"看不到.{0,12}图", re.IGNORECASE), "zh_cant_see_image"),
    (re.compile(r"无法.{0,8}(查看|识别|看|读取).{0,12}(图|图片|图像)"), "zh_cant_view_image"),
    (re.compile(r"视觉(能力)?(受限|有限|不足|无法)"), "zh_vision_limited"),
    (re.compile(r"(图片|图像).{0,8}(看不到|无法.{0,10}看)"), "zh_image_invisible"),
    (re.compile(r"(不能|无法).{0,8}(直接|主动).{0,8}查看"), "zh_cant_directly_view"),
    (re.compile(r"需(要)?(保存|下载).{0,16}(view_image|本地文件).{0,16}(才能|后).{0,8}(看|分析|查看)"), "zh_must_save_first"),
    (re.compile(r"接受.{0,8}视觉.{0,8}受限"), "zh_accepts_vision_limit"),
    (re.compile(r"架构.{0,8}(硬|强).{0,4}限制.{0,12}(图|视觉)"), "zh_architecture_blocks_vision"),

    # English self-capability denial.
    (re.compile(r"\bcan(?:not|'t|\s+not)\s+see\s+(?:chat|pasted|uploaded|inline)\s+image", re.IGNORECASE), "en_cant_see_image"),
    (re.compile(r"\bcan(?:not|'t|\s+not)\s+view\s+(?:\w+\s+){0,3}(?:images?|pictures?)", re.IGNORECASE), "en_cant_view_image"),
    (re.compile(r"\bno\s+vision\s+(?:capability|support)\b", re.IGNORECASE), "en_no_vision"),
    (re.compile(r"\bvision\s+(?:is\s+)?(?:limited|disabled|unavailable)\b", re.IGNORECASE), "en_vision_limited"),
    (re.compile(r"\bunable\s+to\s+(?:see|view|process)\s+(?:images?|pictures?)\b", re.IGNORECASE), "en_unable_to_see"),
    (re.compile(r"\bI\s+(?:lack|don'?t\s+have)\s+(?:vision|image\s+input)", re.IGNORECASE), "en_lack_vision"),
    (re.compile(r"\bimages?\s+(?:are\s+)?(?:not|n't)\s+(?:accessible|visible|available)", re.IGNORECASE), "en_images_inaccessible"),
    (re.compile(r"\bpasted\s+(?:in\s+)?chat\s+(?:are|is)?\s*not?\s+(?:see|view|access)", re.IGNORECASE), "en_pasted_chat_not_seen"),
    (re.compile(r"\bonly\s+screenshots?\s+(?:taken\s+)?(?:by\s+)?(?:agent\s+)?tools?\s+(?:are\s+)?visible", re.IGNORECASE), "en_only_screenshots_visible"),
    (re.compile(r"\b(?:visual|vision)\s+restricted\b", re.IGNORECASE), "en_visual_restricted"),
    (re.compile(r"\bcapability\s+limitations?\s*\([^)]*visual[^)]*\)", re.IGNORECASE), "en_capability_limit_visual"),
)


def is_toxic_self_capability_denial(text: str | None) -> tuple[bool, str | None]:
    """Return ``(True, pattern_id)`` if the text asserts the agent
    cannot do something it actually can (chat-image vision being the
    flagship case). Returns ``(False, None)`` otherwise.

    ``text`` is matched verbatim; we don't lowercase ahead-of-time
    because the regexes themselves carry IGNORECASE where it makes
    sense (English) — Chinese patterns don't need case folding.

    Empty / None text → not toxic (nothing to assert).
    """
    if not text:
        return False, None
    s = str(text)
    for rx, pid in _PATTERNS:
        if rx.search(s):
            return True, pid
    return False, None


__all__ = ["is_toxic_self_capability_denial"]
