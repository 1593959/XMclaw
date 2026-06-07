"""Platform-specific rendering guidance — channel-aware prompt injection.

the upstream agent parity: each IM channel has distinct formatting limits, mention
syntax, and rich-content capabilities. A short platform-hint section lets
the model tailor its output (e.g. use Slack block-kit JSON, split long
replies for Telegram's 4096-char limit, or emit Feishu card payloads)
without bloating the generic persona files.

The guidance is **additive** — persona rules still win on conflict — and
is injected only when the active inbound channel is unambiguously
identified via ``channel_name``.
"""
from __future__ import annotations


#: Short rendering hints keyed by canonical channel id.  Kept terse so
#: they don't bloat the prompt; each addresses one or two known
#: platform quirks.
_GUIDANCE: dict[str, str] = {
    "telegram": (
        "## 当前通道（Telegram）\n\n"
        "- 单条消息上限 4096 字符；超长回复请主动分段。\n"
        "- 支持 MarkdownV2 与 HTML 子集；代码块用 ```language 包裹。\n"
        "- 如需 @ 用户，使用 `@username` 语法；bot 需要知道目标用户的 "
        "username 才能成功提及。"
    ),
    "discord": (
        "## 当前通道（Discord）\n\n"
        "- 支持标准 Markdown、代码高亮、spoiler 标记（||spoiler||）。\n"
        "- 单条消息上限 2000 字符（普通）/ 4000 字符（Nitro）；"
        "超长内容可发多段或上传文件。\n"
        "- 如需 @ 角色或用户，使用 `<@id>` 或 `@Name`；"
        "bot 需要权限才能提及。"
    ),
    "slack": (
        "## 当前通道（Slack）\n\n"
        "- 支持 mrkdwn（`*bold*`、`_italic_`、`` `code` ``、"
        "```多行代码```）。\n"
        "- 单条消息上限 4000 字符；超长内容可拆分为 thread 回复。\n"
        "- 如需 @ 用户，使用 `<@U123>` 语法；bot 需要知道 Member ID。"
    ),
    "feishu": (
        "## 当前通道（飞书 / Lark）\n\n"
        "- 支持富文本、Markdown、卡片消息（interactive card）。\n"
        "- 单条文本消息上限 20000 字符；一般无需手动分段。\n"
        "- 如需 @ 用户，使用 `<at user_id=\"xxx\">@用户名</at>`；"
        "bot 需要知道 open_id。\n"
        "- 群聊中注意区分 @bot 和 @全体；不要在非必要场景使用 @全体。"
    ),
    "lark": (
        "## 当前通道（飞书 / Lark）\n\n"
        "- 支持富文本、Markdown、卡片消息（interactive card）。\n"
        "- 单条文本消息上限 20000 字符；一般无需手动分段。\n"
        "- 如需 @ 用户，使用 `<at user_id=\"xxx\">@用户名</at>`；"
        "bot 需要知道 open_id。\n"
        "- 群聊中注意区分 @bot 和 @全体；不要在非必要场景使用 @全体。"
    ),
    "dingtalk": (
        "## 当前通道（钉钉）\n\n"
        "- 支持 Markdown、ActionCard、FeedCard。\n"
        "- 单条消息上限 5000 字符；超长内容可拆分为多条或上传文件。\n"
        "- 如需 @ 用户，使用 `@手机号` 或 `@staffId`；"
        "群聊机器人需要开启 @ 权限。"
    ),
    "wecom": (
        "## 当前通道（企业微信）\n\n"
        "- 支持文本、Markdown、图文消息。\n"
        "- 单条文本消息上限 2048 字节（约 680 个汉字）；"
        "中文回复尤其要注意长度。\n"
        "- 如需 @ 用户，使用 `@userid` 语法。"
    ),
    "weixin": (
        "## 当前通道（微信）\n\n"
        "- 支持文本、图文、模板消息。\n"
        "- 单条文本消息上限 2048 字节（约 680 个汉字）；"
        "中文回复尤其要注意长度。\n"
        "- 公众号/服务号场景注意 48 小时客服消息窗口限制。"
    ),
    "email": (
        "## 当前通道（Email）\n\n"
        "- 输出纯文本或简洁 Markdown；收件客户端渲染差异大。\n"
        "- 主题行（subject）不会由你生成，只负责正文。\n"
        "- 保持段落简短，方便在移动端阅读。"
    ),
    "acp": (
        "## 当前通道（ACP / Agent Client Protocol）\n\n"
        "- 结构化输出优先：尽量返回 JSON 或标准 Markdown。\n"
        "- 避免平台特定的格式（如 @提及、卡片消息），"
        "因为下游客户端可能不是 IM 应用。"
    ),
}


def platform_guidance(channel_name: str | None) -> str | None:
    """Return the rendering-guidance block for *channel_name*, or
    ``None`` when the channel is unknown / unsupported.
    """
    if not channel_name:
        return None
    return _GUIDANCE.get(channel_name.lower())
