"""MCP stdio server — lightweight JSON-RPC 2.0 implementation.

Speaks the Model Context Protocol (2024-11-05 spec) directly over stdin/stdout.
No external MCP SDK dependency — just Python stdlib + json + asyncio.
"""
from __future__ import annotations

import asyncio
import json
import sys
import traceback
from typing import Any

# Force UTF-8 on Windows — critical for Chinese tool descriptions
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# ── MCP Protocol Constants ──────────────────────────────────────────
PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "xmclaw-mcp"
SERVER_VERSION = "1.0.0"

# ── Tool Definitions ─────────────────────────────────────────────────

TOOLS: list[dict[str, Any]] = [
    # ── Perception ──
    {
        "name": "screen_capture",
        "description": "截取屏幕截图。可指定区域或显示器索引，返回 PNG 图片的 base64 编码。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "region": {
                    "type": "array",
                    "description": "截图区域 [x, y, width, height]，省略则全屏",
                    "items": {"type": "integer"},
                    "minItems": 4, "maxItems": 4,
                },
                "monitor_idx": {
                    "type": "integer",
                    "description": "显示器索引（0=主显示器），默认 0",
                },
            },
        },
    },
    {
        "name": "screen_ocr",
        "description": "对图片进行 OCR 文字识别（基于 RapidOCR，中文识别精度高）。传入图片路径，返回识别出的文字及位置。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "image_path": {
                    "type": "string",
                    "description": "要识别的图片文件绝对路径",
                },
                "lang": {
                    "type": "string",
                    "description": "识别语言：ch（中文）、en（英文）、ch_en（中英混合），默认 ch",
                    "enum": ["ch", "en", "ch_en"],
                },
            },
            "required": ["image_path"],
        },
    },
    {
        "name": "clipboard_read",
        "description": "读取系统剪贴板中的文本内容。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "clipboard_write",
        "description": "将文本写入系统剪贴板。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要写入剪贴板的文本"},
            },
            "required": ["text"],
        },
    },

    # ── Computer Use ──
    {
        "name": "computer_click",
        "description": "鼠标点击指定坐标。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "X 坐标"},
                "y": {"type": "integer", "description": "Y 坐标"},
                "button": {
                    "type": "string",
                    "description": "鼠标按键",
                    "enum": ["left", "right", "middle"],
                },
            },
            "required": ["x", "y"],
        },
    },
    {
        "name": "computer_type",
        "description": "模拟键盘输入文本。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要输入的文本"},
                "interval": {
                    "type": "number",
                    "description": "每个字符之间的间隔（秒），默认 0.01",
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "computer_scroll",
        "description": "鼠标滚轮滚动。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "direction": {
                    "type": "string",
                    "description": "滚动方向",
                    "enum": ["up", "down"],
                },
                "amount": {
                    "type": "integer",
                    "description": "滚动量（clicks），默认 3",
                },
            },
            "required": ["direction"],
        },
    },
    {
        "name": "window_list",
        "description": "列出当前所有可见窗口的标题和位置。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "filter_title": {
                    "type": "string",
                    "description": "按标题关键词过滤，省略则返回全部",
                },
            },
        },
    },
    {
        "name": "window_activate",
        "description": "按标题关键词激活（切换到前台）指定窗口。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "窗口标题关键词（部分匹配）",
                },
            },
            "required": ["title"],
        },
    },

    # ── Browser ──
    {
        "name": "browser_open",
        "description": "用 Playwright 浏览器打开 URL。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "要打开的网页 URL"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "browser_snapshot",
        "description": "获取当前浏览器页面的无障碍树快照（用于理解页面结构）。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "browser_click",
        "description": "点击浏览器页面中的元素。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS 选择器或文本匹配"},
            },
            "required": ["selector"],
        },
    },
    {
        "name": "browser_fill",
        "description": "在浏览器页面表单中填写内容。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "目标 input/textarea 的 CSS 选择器"},
                "value": {"type": "string", "description": "要填入的值"},
            },
            "required": ["selector", "value"],
        },
    },

    # ── IM Send ──
    {
        "name": "im_send",
        "description": "通过已配置的 IM 通道发送消息。支持飞书、钉钉、Telegram 等。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "channel": {
                    "type": "string",
                    "description": "目标通道：feishu / dingtalk / telegram / discord / slack",
                    "enum": ["feishu", "dingtalk", "telegram", "discord", "slack"],
                },
                "chat_id": {
                    "type": "string",
                    "description": "目标会话 ID",
                },
                "content": {
                    "type": "string",
                    "description": "消息内容（支持 Markdown）",
                },
            },
            "required": ["channel", "chat_id", "content"],
        },
    },
    {
        "name": "im_list_channels",
        "description": "列出所有已配置的 IM 通道及其连接状态。",
        "inputSchema": {"type": "object", "properties": {}},
    },

    # ── Memory v2 ──
    {
        "name": "memory_search",
        "description": "在 XMCLaw 的 Memory v2 记忆库中进行语义+关键词混合检索（BM25 + 向量）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索查询"},
                "limit": {"type": "integer", "description": "返回条数上限，默认 10"},
                "kind": {
                    "type": "string",
                    "description": "记忆类型过滤：fact / preference / plan / knowledge，省略则不过滤",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_add_fact",
        "description": "向 XMCLaw 记忆库写入事实。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "事实内容"},
                "kind": {
                    "type": "string",
                    "description": "类型：fact / preference / plan / knowledge",
                    "enum": ["fact", "preference", "plan", "knowledge"],
                },
                "scope": {
                    "type": "string",
                    "description": "作用域：session / workspace / global，默认 global",
                    "enum": ["session", "workspace", "global"],
                },
            },
            "required": ["text", "kind"],
        },
    },

    # ── Health ──
    {
        "name": "xmclaw_health",
        "description": "检查 XMCLaw 各模块健康状态。",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


# ── JSON-RPC 2.0 Implementation ─────────────────────────────────────

def _rpc_response(id_: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _rpc_error(id_: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _write(msg: dict) -> None:
    data = json.dumps(msg, ensure_ascii=False, default=str) + "\n"
    sys.stdout.write(data)
    sys.stdout.flush()


# ── Tool Handlers ────────────────────────────────────────────────────

def _handle_screen_capture(args: dict) -> dict:
    try:
        import mss, base64
    except ImportError:
        return {"ok": False, "error": "mss not installed. Run: pip install xmclaw[computer-use]"}
    region = args.get("region")
    monitor_idx = args.get("monitor_idx", 0)
    with mss.mss() as sct:
        monitors = sct.monitors
        if region and len(region) == 4:
            monitor = {"left": region[0], "top": region[1], "width": region[2], "height": region[3]}
        elif monitor_idx + 1 < len(monitors):
            monitor = monitors[monitor_idx + 1]
        else:
            monitor = monitors[1]
        img = sct.grab(monitor)
        try:
            from PIL import Image
            import io
            pil_img = Image.frombytes("RGB", (img.width, img.height), img.rgb)
            buf = io.BytesIO()
            pil_img.save(buf, format="PNG")
            b64 = base64.b64encode(buf.getvalue()).decode()
        except ImportError:
            b64 = base64.b64encode(img.rgb).decode()
        return {"ok": True, "width": img.width, "height": img.height, "base64": b64, "mime": "image/png"}


def _handle_screen_ocr(args: dict) -> dict:
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        return {"ok": False, "error": "rapidocr-onnxruntime not installed."}
    engine = RapidOCR()
    result, _ = engine(args["image_path"])
    if result is None:
        return {"ok": True, "texts": [], "full_text": ""}
    texts = [{"text": r[1], "confidence": round(r[2], 3), "box": r[0]} for r in result]
    return {"ok": True, "texts": texts, "full_text": "\n".join(t["text"] for t in texts)}


def _handle_clipboard_read(args: dict) -> dict:
    try:
        import pyperclip
    except ImportError:
        return {"ok": False, "error": "pyperclip not installed."}
    return {"ok": True, "text": pyperclip.paste()}


def _handle_clipboard_write(args: dict) -> dict:
    try:
        import pyperclip
    except ImportError:
        return {"ok": False, "error": "pyperclip not installed."}
    pyperclip.copy(args["text"])
    return {"ok": True}


def _handle_computer_click(args: dict) -> dict:
    try:
        import pyautogui
        pyautogui.FAILSAFE = True
    except ImportError:
        return {"ok": False, "error": "pyautogui not installed."}
    x, y = args["x"], args["y"]
    button = args.get("button", "left")
    pyautogui.click(x, y, button=button)
    return {"ok": True, "x": x, "y": y, "button": button}


def _handle_computer_type(args: dict) -> dict:
    try:
        import pyautogui
        pyautogui.FAILSAFE = True
    except ImportError:
        return {"ok": False, "error": "pyautogui not installed."}
    pyautogui.typewrite(args["text"], interval=args.get("interval", 0.01))
    return {"ok": True}


def _handle_computer_scroll(args: dict) -> dict:
    try:
        import pyautogui
        pyautogui.FAILSAFE = True
    except ImportError:
        return {"ok": False, "error": "pyautogui not installed."}
    amount = args.get("amount", 3)
    clicks = amount if args["direction"] == "up" else -amount
    pyautogui.scroll(clicks)
    return {"ok": True}


def _handle_window_list(args: dict) -> dict:
    try:
        import pygetwindow as gw
    except ImportError:
        return {"ok": False, "error": "pygetwindow not installed."}
    ft = args.get("filter_title", "").lower()
    windows = []
    for w in gw.getAllWindows():
        if ft and ft not in w.title.lower():
            continue
        if not w.title.strip():
            continue
        windows.append({"title": w.title, "left": w.left, "top": w.top, "width": w.width, "height": w.height, "active": w.isActive})
    return {"ok": True, "windows": windows, "count": len(windows)}


def _handle_window_activate(args: dict) -> dict:
    try:
        import pygetwindow as gw
    except ImportError:
        return {"ok": False, "error": "pygetwindow not installed."}
    title = args["title"].lower()
    for w in gw.getAllWindows():
        if title in w.title.lower():
            w.activate()
            return {"ok": True, "title": w.title}
    return {"ok": False, "error": "No window matching: " + args["title"]}


# ── Browser (headless, sync API) ────────────────────────────────────

_browser = {"page": None, "browser": None, "pw": None}

def _ensure_browser():
    if _browser["page"] is not None:
        return _browser["page"]
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("playwright not installed. Run: playwright install chromium")
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    page = browser.new_page()
    _browser["pw"] = pw
    _browser["browser"] = browser
    _browser["page"] = page
    return page


def _handle_browser_open(args: dict) -> dict:
    page = _ensure_browser()
    page.goto(args["url"], wait_until="domcontentloaded")
    return {"ok": True, "url": args["url"], "title": page.title()}


def _handle_browser_snapshot(args: dict) -> dict:
    page = _ensure_browser()
    snapshot = page.accessibility.snapshot()
    return {"ok": True, "snapshot": snapshot}


def _handle_browser_click(args: dict) -> dict:
    page = _ensure_browser()
    page.click(args["selector"])
    return {"ok": True, "selector": args["selector"]}


def _handle_browser_fill(args: dict) -> dict:
    page = _ensure_browser()
    page.fill(args["selector"], args["value"])
    return {"ok": True}


# ── IM handlers ─────────────────────────────────────────────────────

def _handle_im_send(args: dict) -> dict:
    channel = args["channel"]
    chat_id = args["chat_id"]
    content = args["content"]
    try:
        if channel == "feishu":
            return _im_send_feishu(chat_id, content)
        elif channel == "dingtalk":
            return {"ok": False, "error": "DingTalk requires daemon. Start: xmclaw serve"}
        elif channel == "telegram":
            return _im_send_telegram(chat_id, content)
        elif channel == "discord":
            return {"ok": False, "error": "Discord requires daemon. Start: xmclaw serve"}
        elif channel == "slack":
            return _im_send_slack(chat_id, content)
        else:
            return {"ok": False, "error": "Unknown channel: " + channel}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _im_send_feishu(chat_id: str, content: str) -> dict:
    try:
        import lark_oapi as lark
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest, CreateMessageRequestBody,
        )
    except ImportError:
        return {"ok": False, "error": "lark-oapi not installed."}
    cfg = _load_channel_config("feishu")
    if not cfg or not cfg.get("enabled"):
        return {"ok": False, "error": "Feishu channel not configured"}
    client = lark.Client.builder().app_id(cfg["app_id"]).app_secret(cfg["app_secret"]).build()

    # Detect card (JSON) vs plain text
    stripped = content.strip()
    if stripped.startswith("{"):
        body_content = stripped
        msg_type = "interactive"
    else:
        body_content = json.dumps({"text": stripped}, ensure_ascii=False)
        msg_type = "text"

    req = (
        CreateMessageRequest.builder()
        .receive_id_type("chat_id")
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .content(body_content)
            .msg_type(msg_type)
            .build()
        )
        .build()
    )
    resp = client.im.v1.message.create(req)
    if not resp.success():
        return {"ok": False, "error": f"Feishu API error: code={resp.code} msg={resp.msg}"}
    msg_id = getattr(resp.data, "message_id", "") if resp.data else ""
    return {"ok": True, "message_id": msg_id}


def _im_send_telegram(chat_id: str, content: str) -> dict:
    import requests
    cfg = _load_channel_config("telegram")
    if not cfg or not cfg.get("enabled"):
        return {"ok": False, "error": "Telegram channel not configured"}
    url = "https://api.telegram.org/bot" + cfg["bot_token"] + "/sendMessage"
    resp = requests.post(url, json={"chat_id": chat_id, "text": content}, timeout=10)
    return {"ok": resp.status_code == 200, "data": resp.json()}


def _im_send_slack(chat_id: str, content: str) -> dict:
    try:
        from slack_sdk import WebClient
    except ImportError:
        return {"ok": False, "error": "slack-sdk not installed."}
    cfg = _load_channel_config("slack")
    if not cfg or not cfg.get("enabled"):
        return {"ok": False, "error": "Slack channel not configured"}
    WebClient(token=cfg["bot_token"]).chat_postMessage(channel=chat_id, text=content)
    return {"ok": True}


def _handle_im_list_channels(args: dict) -> dict:
    cfg = _load_config()
    channels_cfg = cfg.get("channels", {})
    result = {}
    for name in ["feishu", "dingtalk", "telegram", "discord", "slack"]:
        ch = channels_cfg.get(name, {})
        result[name] = {"enabled": ch.get("enabled", False), "configured": bool(ch)}
    return {"ok": True, "channels": result}


# ── Memory v2 ───────────────────────────────────────────────────────

def _handle_memory_search(args: dict) -> dict:
    try:
        from xmclaw.memory.v2.service import MemoryService
        svc = MemoryService.from_config(_load_config())
        facts = asyncio.run(svc.recall_hybrid(args["query"], top_k=args.get("limit", 10), kind_filter=args.get("kind")))
        return {"ok": True, "results": [{"id": f.id, "text": f.text, "kind": f.kind, "score": getattr(f, "score", None)} for f in facts], "count": len(facts)}
    except Exception as e:
        return {"ok": False, "error": "Memory search failed: " + str(e)}


def _handle_memory_add_fact(args: dict) -> dict:
    try:
        from xmclaw.memory.v2.service import MemoryService
        from xmclaw.memory.v2.models import FactCreate
        svc = MemoryService.from_config(_load_config())
        fact = asyncio.run(svc.upsert(FactCreate(text=args["text"], kind=args["kind"], scope=args.get("scope", "global"))))
        return {"ok": True, "fact_id": fact.id}
    except Exception as e:
        return {"ok": False, "error": "Memory add failed: " + str(e)}


# ── Health ──────────────────────────────────────────────────────────

def _handle_xmclaw_health(args: dict) -> dict:
    modules = {}
    for mod_name in ["mss", "pyautogui", "rapidocr_onnxruntime", "playwright", "lancedb", "pyperclip", "lark_oapi"]:
        try:
            __import__(mod_name)
            modules[mod_name] = "available"
        except ImportError:
            modules[mod_name] = "not_installed"
    return {"ok": True, "server": SERVER_NAME + " v" + SERVER_VERSION, "protocol": PROTOCOL_VERSION, "tools_count": len(TOOLS), "modules": modules}


# ── Helpers ─────────────────────────────────────────────────────────

def _load_config() -> dict:
    import os
    from pathlib import Path
    paths = [os.environ.get("XMC_CONFIG_PATH"), Path.home() / ".xmclaw" / "daemon" / "config.json", Path("daemon") / "config.json"]
    for p in paths:
        if p and Path(p).exists():
            return json.loads(Path(p).read_text(encoding="utf-8"))
    return {}


def _load_channel_config(channel: str) -> dict:
    return _load_config().get("channels", {}).get(channel, {})


# ── Tool Router ─────────────────────────────────────────────────────

HANDLERS: dict[str, Any] = {
    "screen_capture": _handle_screen_capture,
    "screen_ocr": _handle_screen_ocr,
    "clipboard_read": _handle_clipboard_read,
    "clipboard_write": _handle_clipboard_write,
    "computer_click": _handle_computer_click,
    "computer_type": _handle_computer_type,
    "computer_scroll": _handle_computer_scroll,
    "window_list": _handle_window_list,
    "window_activate": _handle_window_activate,
    "browser_open": _handle_browser_open,
    "browser_snapshot": _handle_browser_snapshot,
    "browser_click": _handle_browser_click,
    "browser_fill": _handle_browser_fill,
    "im_send": _handle_im_send,
    "im_list_channels": _handle_im_list_channels,
    "memory_search": _handle_memory_search,
    "memory_add_fact": _handle_memory_add_fact,
    "xmclaw_health": _handle_xmclaw_health,
}


def _handle_tools_call(id_: Any, name: str, arguments: dict) -> None:
    handler = HANDLERS.get(name)
    if handler is None:
        _write(_rpc_error(id_, -32601, "Tool not found: " + name))
        return
    try:
        result = handler(arguments)
    except Exception as e:
        _write(_rpc_error(id_, -32000, "Tool error: " + str(e)))
        return
    text = json.dumps(result, ensure_ascii=False, default=str)
    _write(_rpc_response(id_, {"content": [{"type": "text", "text": text}], "isError": not result.get("ok", True)}))


# ── Main Loop ───────────────────────────────────────────────────────

def _process_message(msg: dict) -> None:
    method = msg.get("method", "")
    id_ = msg.get("id")

    if method == "initialize":
        _write(_rpc_response(id_, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }))
    elif method == "notifications/initialized":
        pass
    elif method == "tools/list":
        _write(_rpc_response(id_, {"tools": TOOLS}))
    elif method == "tools/call":
        params = msg.get("params", {})
        _handle_tools_call(id_, params.get("name", ""), params.get("arguments", {}))
    elif method == "ping":
        _write(_rpc_response(id_, {}))
    else:
        _write(_rpc_error(id_, -32601, "Method not found: " + method))


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            _process_message(json.loads(line))
        except json.JSONDecodeError:
            pass


if __name__ == "__main__":
    main()
