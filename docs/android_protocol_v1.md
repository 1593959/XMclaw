# XMclaw Android Companion Protocol v1

> **Status**: Draft — 2026-06-18  
> **Scope**: 手机端无障碍 App ↔ XMclaw daemon 双向控制唯一契约。两端共享此文档，任何 schema 变更需同步修改。  
> **Companion**: 实现指南见 `docs/ANDROID_COMPANION_DEV_GUIDE_2026.md`；设计/选型见 `docs/ANDROID_COMPANION_DESIGN_2026.md`。

---

## 1. 传输层

- **端点**: `GET /device/v1/{device_id}?token=<pairing_token>`
- **传输**: WebSocket (UTF-8 JSON 文本帧)
- **鉴权**: 复用现有 `pairing_token` 共享密钥。失败 → 关闭 code=4401；device_id 未配对 → 4403。
- **大二进制**: 截图不走 WS。手机 POST 到 `/api/v2/uploads` → 拿 URL → 在 `obs.screenshot` 帧里只带 URL。

---

## 2. 统一帧格式

```json
{
  "v": 1,
  "type": "<namespace.name>",
  "req_id": "<uuid|null>",
  "ts": 1781700000.12,
  "data": { }
}
```

- `v`: 协议版本，当前固定为 `1`。
- `type`: 帧类型，见 §3 / §4。
- `req_id`: 需要回执的请求带 UUID；应答用同一 `req_id`。
- `ts`: epoch 秒（float）。
- `data`: 载荷，类型相关。

---

## 3. 握手

```
手机 →  dev.hello
{
  "device_id": "d-abc123",
  "name": "XMclaw Companion",
  "model": "Pixel 8",
  "android": "14",
  "app_ver": "1.0.0",
  "perms": {
    "accessibility": true,
    "projection": false,
    "notifications": true
  },
  "screen": { "w": 1080, "h": 2400, "density": 2.75 }
}

daemon → dev.welcome
{
  "server_ver": "2.4.0",
  "capabilities": ["ui_tree", "screenshot", "clipboard", "gesture"],
  "heartbeat_s": 20
}
```

---

## 4. 下行（daemon → 手机）动作 —— 规范命令集

下行帧统一为 `{"v":1,"type":"cmd","req_id":"...","ts":...,"data":<命令>}`，其中 `<命令>` 就是下表的 JSON（`{"ui": ...}` 或 `{"clipboard_cmd": ...}`）。手机端按 `ui`/`clipboard_cmd` 分派。**这是两端唯一动作契约。**

| 操作 | 命令（`data`）| 回执 |
|---|---|---|
| 打开应用 | `{"ui":"open_app","package_name":"com.taobao.idlefish"}` | `act.result{ok,error?}` |
| 点击元素 | `{"ui":"click","target":{"text":"搜索"}}` | `act.result{ok, matched?}` |
| 点击坐标 | `{"ui":"tap","x":540,"y":1200}` | `act.result{ok}` |
| 输入文字 | `{"ui":"input","text":"下午好","index":0}` | `act.result{ok}`（setText 原生中文）|
| 滑动 | `{"ui":"swipe","x1":540,"y1":1800,"x2":540,"y2":600,"ms":300}` | `act.result{ok}` |
| 按键 | `{"ui":"key_event","key":"KEYCODE_BACK"}` / `KEYCODE_HOME` … | `act.result{ok}` |
| 截图 | `{"ui":"screenshot"}` | `obs.screenshot{url,w,h}` |
| 获取 UI 树 | `{"ui":"tree","clickable_only":false}` | `obs.tree{nodes,pkg,activity}` |
| 通知栏 | `{"ui":"notification"}` | `act.result{ok}`（拉下通知栏；读通知内容走 `obs.event`）|
| 长按 | `{"ui":"long_press","x":540,"y":1200,"ms":600}` 或 `{"target":{...}}` | `act.result{ok}` |
| 等待元素 | `{"ui":"wait","event":"exists","target":{"text":"完成"},"timeout_ms":5000}` | `act.result{ok, found:bool, waited_ms}` |
| 剪贴板-读 | `{"clipboard_cmd":"get_clipboard"}` | `obs.clipboard{text}` |
| 剪贴板-写 | `{"clipboard_cmd":"set_clipboard","text":"内容"}` | `act.result{ok}` |
| 持续感知开关 | `{"ui":"observe","on":true,"on_window_change":true,"min_interval_ms":800}` | `act.result{ok}` |

约定：
- **`key`** 用原生 `KEYCODE_*`（BACK/HOME/APP_SWITCH/ENTER/DEL…）；daemon 工具层接受友好名(back/home/recents)并映射。
- **`event`**（wait）取值：`exists`（出现）/ `gone`（消失）。
- **`index`**（input）：当页面多个可编辑框时选第几个（默认 0 = 当前焦点框）。

### 4.1 `target` 元素选择器（click / wait / long_press 用）

```json
{"text":"搜索"}                         // 文本精确/包含
{"res_id":"com.x:id/search_btn"}        // resource-id
{"desc":"搜索按钮"}                      // content-desc
{"text":"搜索","index":1}               // 同名多个时取第几个
{"xpath":"//*[@text='搜索']"}           // 兜底（手机端可选支持）
```

匹配优先级：res_id > text 精确 > desc > text 包含。命中后手机端用节点 `ACTION_CLICK`（点不动再退 center 手势）。

---

## 5. 上行（手机 → daemon）感知 + 指令

| type | data |
|---|---|
| `obs.tree` | `{nodes:[Node], pkg, activity}`（Node 见 §6）|
| `obs.screenshot` | `{url, w, h}` |
| `obs.clipboard` | `{text}`（应答 `get_clipboard`）|
| `obs.event` | `{kind: window_changed\|notification\|toast\|app_opened, pkg?, text?}` |
| `act.result` | `{ok:bool, error?:str, extra?:{}}` |
| `user.message` | `{text, image_urls?:[]}`（用户从手机给 agent 下指令）|
| `user.approval` | `{request_id, decision: allow\|always\|deny}` |

---

## 6. Node DTO（无障碍节点）

```json
{
  "id": "n12",                     // 本帧内稳定引用
  "text": "WLAN",
  "res_id": "com.android.settings:id/title",
  "desc": "",                      // content-desc
  "cls": "android.widget.TextView",
  "clickable": true,
  "editable": false,
  "bounds": [0,210,1080,360],      // x1,y1,x2,y2
  "center": [540,285]
}
```

- `id`: 本帧内自增（n0,n1,…）。服务端**只在同一帧内**用 node_id 回点；跨帧动作用 center 坐标或 res_id+text 重新定位（避免 id 失效）。
- `bounds`: 屏幕物理像素坐标 `[x1, y1, x2, y2]`。
- `center`: `[cx, cy]` 用于兜底坐标点击。

---

## 7. 错误码 / 约束

- `act.result.error` 文案直接透传给 agent。
- 动作被黑名单拦截 → `act.result{ok:false, error:"blocked: sensitive app <pkg>"}`。
- daemon 对每个下行请求设超时（默认 15s）；超时回工具层 `ToolResult(ok=false, error="device timeout")`。
- WebSocket 关闭码：
  - `4401` — 鉴权失败（token 无效）
  - `4403` — device_id 未配对
  - `4400` — 协议版本不匹配

---

## 8. 端到端时序样例：「打开设置，进 WLAN」

```
agent 看屏幕 → tool phone_screenshot
  daemon → 手机  {"ui":"screenshot"}              (r1)
  手机 → uploads POST png → url；手机 → daemon obs.screenshot{url} (r1) → attach_image_url → LLM 看到桌面
agent 开设置 → tool phone_open_app{package_name:"com.android.settings"}
  daemon → 手机  {"ui":"open_app","package_name":"com.android.settings"}
  手机 onWindowStateChanged → obs.event window_changed
agent 读可点元素 → tool phone_ui_tree{clickable_only:true}
  daemon → 手机  {"ui":"tree","clickable_only":true}
  手机 → daemon  obs.tree {nodes:[... {text:"WLAN", res_id:".../title", center:[540,285]} ...]}
agent 点 WLAN（按元素，不猜坐标）→ tool phone_click{target:{text:"WLAN"}}
  daemon → 手机  {"ui":"click","target":{"text":"WLAN"}}
  手机 节点 ACTION_CLICK → act.result{ok:true, matched:"WLAN"}
agent 等列表出现 → tool phone_wait{event:"exists", target:{text:"已连接"}, timeout_ms:5000}
  daemon → 手机  {"ui":"wait","event":"exists","target":{"text":"已连接"},"timeout_ms":5000}
  手机 → act.result{ok:true, found:true}
agent 截图确认 → tool phone_screenshot → 完成
```

---

## 9. 版本历史

| 版本 | 日期 | 变更 |
|---|---|---|
| v1.0 | 2026-06-18 | 初始草案 — M0/M1 骨架 |
