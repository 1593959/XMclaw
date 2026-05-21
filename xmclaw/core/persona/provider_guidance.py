"""Provider-family operational guidance — model-specific prompt injection.

Hermes parity: each backend family (GPT / Claude / Google) has distinct
behavioural quirks that are best corrected with a short operational-hint
section rather than polluting the generic persona files.

The guidance is **additive** — persona rules still win on conflict — and
is injected only when the active backend is unambiguously identified via
``backend_label``.
"""
from __future__ import annotations


#: Mapping from provider slug (as it appears in ``backend_label``) to
#: canonical family name.  Families group providers that share the same
#: operational quirks (e.g. OpenAI + Azure OpenAI both follow GPT
#: function-calling conventions).
_PROVIDER_TO_FAMILY: dict[str, str] = {
    "openai": "gpt",
    "azure_openai": "gpt",
    "anthropic": "claude",
    "google": "google",
    "gemini": "google",
}

#: Short operational hints keyed by family.  Kept terse so they don't
#: bloat the prompt; each addresses one or two known failure modes.
_GUIDANCE: dict[str, str] = {
    "gpt": (
        "## 后端操作提示（OpenAI/GPT）\n\n"
        "- 工具调用：严格遵循 JSON Schema，参数必须是合法 JSON，"
        "不要包裹 markdown 代码块（如 ```json）。\n"
        "- 多步推理：如有 reasoning 类型支持，优先使用它；"
        "不要在最终回复里输出思考过程。\n"
        "- 代码生成：优先使用标准库；需要第三方包时先确认环境可用。"
    ),
    "claude": (
        "## 后端操作提示（Anthropic/Claude）\n\n"
        "- 你可以使用 computer use / 代码执行工具操作文件、浏览网页、运行代码。\n"
        "- 规划纪律：复杂行动前先输出 1–2 句简要计划，再分步执行。\n"
        "- 诚实优先：不确定时直接说明，不要编造事实或文件路径。"
    ),
    "google": (
        "## 后端操作提示（Google/Gemini）\n\n"
        "- Safety filter 可能误拦截无害内容；若工具调用返回安全错误，"
        "可尝试用更中性措辞重试。\n"
        "- 代码执行：优先通过 python 工具运行；Gemini 原生支持代码解释。\n"
        "- 长上下文：Gemini 支持 1M+ token 上下文，遇到大文件可放心全读。"
    ),
}


def _provider_family_from_label(label: str | None) -> str | None:
    """Extract provider family from a backend label string.

    Expected label shapes::

        "openai/gpt-4o (default)"
        "anthropic/claude-sonnet-4 (claude)"
        "google/gemini-2.5-pro (gemini)"

    Returns the canonical family (``gpt`` | ``claude`` | ``google``) or
    ``None`` when the provider can't be mapped.
    """
    if not label:
        return None
    # The provider is the first slash-delimited segment.
    provider = label.split("/")[0].strip().lower()
    return _PROVIDER_TO_FAMILY.get(provider)


def provider_guidance(backend_label: str | None) -> str | None:
    """Return the operational-guidance block for *backend_label*, or
    ``None`` when the provider family is unknown / unsupported.
    """
    family = _provider_family_from_label(backend_label)
    if family is None:
        return None
    return _GUIDANCE.get(family)
