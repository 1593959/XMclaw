# XMclaw 安卓伴侣 — 开发实现指南（Dev Guide）2026-06

> **配套**: 设计/选型见 [ANDROID_COMPANION_DESIGN_2026.md](ANDROID_COMPANION_DESIGN_2026.md)（为什么这样做）。
> **本文**: 怎么做——完整协议契约、两端代码骨架、文件树、构建/测试/里程碑任务拆解。
> 目标：照着这份文档能一步步把「手机端无障碍 App ↔ XMclaw daemon 双向控制」实现出来。
> **范围**: kimiclaw 同款的**手机端 App** 路线（大脑复用 daemon）。ADB 版 `providers/tool/android.py` 仅回退/联调。

---

## 0. 前置环境

**手机端**
- Android Studio (Koala+)、JDK 17、Kotlin 2.0、Gradle 8.x、minSdk 26（8.0）/ targetSdk 34。
- 一台真机（开发者模式 + 允许安装未知来源）或模拟器（无障碍/投屏在模拟器可用）。

**daemon 端**
- 现有 XMclaw 开发环境（`pip install -e ".[dev]"`）。
- daemon 跑在与手机同一局域网（M1 只做局域网，见设计 §11 决策 2）。

**约定**
- 协议帧 schema 是两端唯一契约，落在 `docs/android_protocol_v1.md`（本文 §2 即其内容来源）。
- 所有时间戳 = epoch 秒（float）；所有坐标 = 屏幕物理像素。

---

## 1. 工程结构

### 1.1 手机端：独立仓库 `xmclaw-companion`（推荐，见设计 §11 决策 1）

```
xmclaw-companion/
├── app/
│   ├── build.gradle.kts
│   └── src/main/
│       ├── AndroidManifest.xml
│       └── java/com/xmclaw/companion/
│           ├── CompanionApp.kt                # Application
│           ├── ui/
│           │   ├── CompanionActivity.kt       # 配对/权限引导/任务视图入口
│           │   ├── PairingScreen.kt           # 扫码/填地址+配对码 (Compose)
│           │   └── TaskMirrorScreen.kt        # Mission Control 移动投影 (M3)
│           ├── service/
│           │   ├── XmclawAccessibilityService.kt   # 手眼核心
│           │   ├── ScreenCaptureService.kt         # MediaProjection 截图
│           │   └── CompanionForegroundService.kt   # 常驻 + 持 WS
│           ├── link/
│           │   ├── DaemonLink.kt              # OkHttp WebSocket 客户端 + 重连
│           │   ├── Frame.kt                   # 协议帧编解码 (kotlinx.serialization)
│           │   └── Pairing.kt                 # 配对令牌存取 (EncryptedSharedPreferences)
│           ├── act/
│           │   ├── Actuator.kt                # 执行 act.* (dispatchGesture/global/setText)
│           │   └── Blocklist.kt               # 敏感 App 写动作拦截
│           └── perceive/
│               ├── TreeReader.kt              # rootInActiveWindow → 扁平节点 DTO
│               └── NodeRef.kt                 # 稳定 node_id 方案
└── README.md
```

### 1.2 daemon 端：XMclaw 仓库内新增（Python）

```
xmclaw/daemon/
├── routers/device.py            # /device/v1/{device_id} WebSocket 路由
└── device_registry.py           # DeviceRegistry：device_id ⇄ 活动连接 + 请求/应答配对
xmclaw/providers/tool/
└── android_remote.py            # AndroidRemoteToolProvider：把控手机暴露成 agent 工具
tests/unit/
├── test_v2_device_registry.py
└── test_v2_android_remote_tools.py
docs/
└── android_protocol_v1.md       # 两端共享的帧契约（从本文 §2 抽出）
```

> daemon 侧只加「连接管理 + 工具桥」；大脑（AgentLoop/LLM/记忆/技能）一行不改地复用。

---

## 2. 协议契约 v1（两端唯一真相）

### 2.1 传输

- WS 端点：`GET /device/v1/{device_id}?token=<pairing_token>`（沿用现有 `pairing_token` 鉴权）。
- 帧：UTF-8 JSON 文本帧。统一信封：

```json
{ "v": 1, "type": "<namespace.name>", "req_id": "<uuid|null>", "ts": 1781700000.12, "data": { } }
```

- 需要回执的请求带 `req_id`；应答用同一 `req_id` 回 `act.result` / `obs.*`。
- 大二进制（截图）**不走 WS**：手机 POST 到 `/api/v2/uploads`（复用现有上传）→ 拿 URL → 在 `obs.screenshot` 里只带 URL。

### 2.2 握手

```
手机 →  dev.hello   {device_id, name, model, android, app_ver,
                     perms:{accessibility:true, projection:false, notifications:true},
                     screen:{w,h,density}}
daemon → dev.welcome {server_ver, capabilities:[...], heartbeat_s:20}
```
鉴权失败 → daemon 关闭连接 code=4401。device_id 未配对 → 4403。

### 2.3 下行（daemon → 手机）动作 —— **规范命令集（canonical）**

下行帧统一为 `{"v":1,"type":"cmd","req_id":"...","ts":...,"data": <命令>}`，其中 `<命令>` 就是下表的
JSON（`{"ui": ...}` 或 `{"clipboard_cmd": ...}`）。手机端按 `ui`/`clipboard_cmd` 分派。**这是两端唯一动作契约。**

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

### 2.3.1 `target` 元素选择器（click / wait / long_press 用）

```json
{"text":"搜索"}                         // 文本精确/包含
{"res_id":"com.x:id/search_btn"}        // resource-id
{"desc":"搜索按钮"}                      // content-desc
{"text":"搜索","index":1}               // 同名多个时取第几个
{"xpath":"//*[@text='搜索']"}           // 兜底（手机端可选支持）
```
匹配优先级：res_id > text 精确 > desc > text 包含。命中后手机端用节点 `ACTION_CLICK`（点不动再退 center 手势）。

### 2.4 上行（手机 → daemon）感知 + 指令

| type | data |
|---|---|
| `obs.tree` | `{nodes:[Node], pkg, activity}`（Node 见 §2.5）|
| `obs.screenshot` | `{url, w, h}` |
| `obs.clipboard` | `{text}`（应答 `get_clipboard`）|
| `obs.event` | `{kind: window_changed\|notification\|toast\|app_opened, pkg?, text?}` |
| `act.result` | `{ok:bool, error?:str, extra?:{}}` |
| `user.message` | `{text, image_urls?:[]}`（用户从手机给 agent 下指令）|
| `user.approval` | `{request_id, decision: allow\|always\|deny}` |

### 2.5 Node DTO（无障碍节点）

```json
{
  "id": "n12",                     // 本帧内稳定引用（见 §5 NodeRef）
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

### 2.6 错误码 / 约束
- `act.result.error` 文案直接透传给 agent（与 daemon `utils/http_errors` 风格一致）。
- 动作被黑名单拦截 → `act.result{ok:false, error:"blocked: sensitive app <pkg>"}`。
- daemon 对每个下行请求设超时（默认 15s）；超时回工具层 `ToolResult(ok=false, error="device timeout")`。

---

## 3. daemon 侧实现（Python）

### 3.1 `DeviceRegistry`

```python
# xmclaw/daemon/device_registry.py
class DeviceRegistry:
    def __init__(self) -> None:
        self._conns: dict[str, "DeviceConn"] = {}      # device_id -> conn
    def register(self, device_id: str, ws) -> "DeviceConn": ...
    def drop(self, device_id: str) -> None: ...
    def get(self, device_id: str | None) -> "DeviceConn | None":
        # device_id None 且只有一台 → 返回那台（单设备便利）
        ...
    def list(self) -> list[dict]: ...

class DeviceConn:
    """一台已连手机。send_request 发下行帧并 await 对应 req_id 的应答。"""
    async def send_request(self, type_: str, data: dict, *, timeout=15.0) -> dict:
        req_id = uuid4().hex
        fut = self._loop.create_future()
        self._pending[req_id] = fut
        await self._ws.send_json({"v":1,"type":type_,"req_id":req_id,"ts":time(),"data":data})
        return await asyncio.wait_for(fut, timeout)   # 由 reader 在收到 act.result/obs.* 时 set
    def resolve(self, req_id: str, payload: dict) -> None:
        fut = self._pending.pop(req_id, None)
        if fut and not fut.done(): fut.set_result(payload)
```

### 3.2 `/device/v1/{device_id}` 路由

```python
# xmclaw/daemon/routers/device.py
@router.websocket("/device/v1/{device_id}")
async def device_ws(ws: WebSocket, device_id: str):
    if not _check_pairing_token(ws):           # 复用现有配对令牌校验
        await ws.close(code=4401); return
    await ws.accept()
    conn = registry.register(device_id, ws)
    try:
        async for raw in ws.iter_json():
            t = raw.get("type"); data = raw.get("data") or {}; rid = raw.get("req_id")
            if t in ("act.result","obs.tree","obs.screenshot") and rid:
                conn.resolve(rid, data)                       # 解开等待的下行请求
            elif t == "user.message":
                await _inject_user_message(device_id, data)   # 注入 AgentLoop（见 3.4）
            elif t == "user.approval":
                approval_service.respond(data["request_id"], data["decision"])
            elif t == "obs.event":
                bus.publish(EventType.DEVICE_EVENT, {"device_id":device_id, **data})
            elif t == "dev.hello":
                conn.set_hello(data); await ws.send_json(_welcome())
    finally:
        registry.drop(device_id)
```

### 3.3 `AndroidRemoteToolProvider`

LLM 工具一一对应 §2.3 规范命令集；invoke 里把工具参数打包成 `{"ui":...}`/`{"clipboard_cmd":...}` 命令
经 `DeviceConn.send_request` 下发。**工具名 ↔ 命令** 映射：

| LLM 工具 | 下发命令 | 应答 |
|---|---|---|
| `phone_open_app{package_name}` | `{"ui":"open_app",...}` | act.result |
| `phone_click{target}` | `{"ui":"click",...}` | act.result |
| `phone_tap{x,y}` | `{"ui":"tap",...}` | act.result |
| `phone_input{text,index?}` | `{"ui":"input",...}` | act.result |
| `phone_swipe{x1,y1,x2,y2,ms?}` | `{"ui":"swipe",...}` | act.result |
| `phone_key{key}` | `{"ui":"key_event",...}`（友好名→KEYCODE_*）| act.result |
| `phone_screenshot{}` | `{"ui":"screenshot"}` | obs.screenshot → attach_image |
| `phone_ui_tree{clickable_only?}` | `{"ui":"tree",...}` | obs.tree |
| `phone_notification{}` | `{"ui":"notification"}` | act.result |
| `phone_wait{event,target,timeout_ms?}` | `{"ui":"wait",...}` | act.result{found} |
| `phone_clipboard_get{}` | `{"clipboard_cmd":"get_clipboard"}` | obs.clipboard |
| `phone_clipboard_set{text}` | `{"clipboard_cmd":"set_clipboard",...}` | act.result |

```python
# xmclaw/providers/tool/android_remote.py
_KEY_ALIASES = {"back":"KEYCODE_BACK","home":"KEYCODE_HOME","recents":"KEYCODE_APP_SWITCH",
                "enter":"KEYCODE_ENTER","delete":"KEYCODE_DEL"}

class AndroidRemoteToolProvider(ToolProvider):
    def __init__(self, registry: DeviceRegistry): self._reg = registry
    def list_tools(self) -> list[ToolSpec]:
        return [_PHONE_OPEN_APP, _PHONE_CLICK, _PHONE_TAP, _PHONE_INPUT, _PHONE_SWIPE,
                _PHONE_KEY, _PHONE_SCREENSHOT, _PHONE_UI_TREE, _PHONE_NOTIFICATION,
                _PHONE_WAIT, _PHONE_CLIP_GET, _PHONE_CLIP_SET]
    async def invoke(self, call: ToolCall) -> ToolResult:
        a = call.args or {}
        conn = self._reg.get(a.get("device_id"))
        if conn is None:
            return _fail(call, "no paired phone connected (open the companion app)")
        cmd, kind = self._to_command(call.name, a)   # 工具→规范命令 + 期望应答类型
        try:
            r = await conn.send_request("cmd", cmd)   # data=cmd（{"ui":..} / {"clipboard_cmd":..}）
        except asyncio.TimeoutError:
            return _fail(call, "device timeout")
        if kind == "image":                            # screenshot
            return ToolResult(call_id=call.id, ok=True, content=json.dumps(r, ensure_ascii=False),
                              metadata={"attach_image_url": r.get("url")})
        if not r.get("ok", True) and "error" in r:
            return _fail(call, r["error"])             # 厂商/拦截错误原样透传
        return _ok(call, json.dumps(r, ensure_ascii=False))

    def _to_command(self, name: str, a: dict) -> tuple[dict, str]:
        if name == "phone_open_app": return {"ui":"open_app","package_name":a["package_name"]}, "ack"
        if name == "phone_click":    return {"ui":"click","target":a["target"]}, "ack"
        if name == "phone_tap":      return {"ui":"tap","x":a["x"],"y":a["y"]}, "ack"
        if name == "phone_input":    return {"ui":"input","text":a["text"],"index":a.get("index",0)}, "ack"
        if name == "phone_swipe":    return {"ui":"swipe","x1":a["x1"],"y1":a["y1"],"x2":a["x2"],"y2":a["y2"],"ms":a.get("ms",300)}, "ack"
        if name == "phone_key":      return {"ui":"key_event","key":_KEY_ALIASES.get(str(a["key"]).lower(), a["key"])}, "ack"
        if name == "phone_screenshot": return {"ui":"screenshot"}, "image"
        if name == "phone_ui_tree":  return {"ui":"tree","clickable_only":a.get("clickable_only",False)}, "tree"
        if name == "phone_notification": return {"ui":"notification"}, "ack"
        if name == "phone_wait":     return {"ui":"wait","event":a.get("event","exists"),"target":a["target"],"timeout_ms":a.get("timeout_ms",5000)}, "ack"
        if name == "phone_clipboard_get": return {"clipboard_cmd":"get_clipboard"}, "ack"
        if name == "phone_clipboard_set": return {"clipboard_cmd":"set_clipboard","text":a["text"]}, "ack"
        raise ValueError(name)
```

截图工具用 `metadata.attach_image_url`（或先下到 uploads 再用现有 `attach_image`）让 agent **看到**手机屏幕。

### 3.4 上行用户指令接入 AgentLoop
`user.message` → 取该 device 绑定的 session_id → 调 `agent.run_turn(session_id, text, images=...)`，
与人类从 Web/CLI 下指令同一入口。`user.approval` → 现有 `approval_service`。

### 3.5 接线 + 门控
- factory：`tools.android_companion.enabled`（默认关）→ 注册 `AndroidRemoteToolProvider`（传入全局 `DeviceRegistry`）。
- app_lifespan：构造单例 `DeviceRegistry` 挂 `app.state`，`/device/v1` 路由引用它。
- 复用 `security.tool_guard`：高风险 `phone_text`/支付页动作过确认网关。

### 3.6 daemon 侧测试（无真机）
- `test_v2_device_registry.py`：mock WS，验证 send_request/resolve 配对、超时、单设备便利 get。
- `test_v2_android_remote_tools.py`：mock DeviceConn，验证每个工具发对帧 + 解析 act.result/obs.* + 截图 attach + 无设备降级。

---

## 4. 手机端实现（Kotlin）

### 4.1 Manifest 关键项

```xml
<uses-permission android:name="android.permission.FOREGROUND_SERVICE"/>
<uses-permission android:name="android.permission.FOREGROUND_SERVICE_MEDIA_PROJECTION"/>
<uses-permission android:name="android.permission.POST_NOTIFICATIONS"/>
<uses-permission android:name="android.permission.QUERY_ALL_PACKAGES"/> <!-- 列 App，可选 -->
<service android:name=".service.XmclawAccessibilityService"
         android:permission="android.permission.BIND_ACCESSIBILITY_SERVICE"
         android:exported="false">
  <intent-filter><action android:name="android.accessibilityservice.AccessibilityService"/></intent-filter>
  <meta-data android:name="android.accessibilityservice" android:resource="@xml/accessibility_config"/>
</service>
```
`accessibility_config.xml`：`canPerformGestures="true"`、`canRetrieveWindowContent="true"`、
`accessibilityEventTypes="typeWindowStateChanged|typeNotificationStateChanged|..."`、`accessibilityFlags="flagReportViewIds|flagRetrieveInteractiveWindows"`。

### 4.2 `XmclawAccessibilityService`（手眼）

```kotlin
class XmclawAccessibilityService : AccessibilityService() {
  override fun onAccessibilityEvent(e: AccessibilityEvent) {
    when (e.eventType) {
      TYPE_WINDOW_STATE_CHANGED -> link.send(Frame.event("window_changed", pkg=e.packageName))
      TYPE_NOTIFICATION_STATE_CHANGED -> link.send(Frame.event("notification", text=e.text.toString()))
    }
  }
  // 由 DaemonLink 收到下行帧后回调到这里：
  fun readTree(clickableOnly: Boolean): List<Node> =
      TreeReader.flatten(rootInActiveWindow, clickableOnly)         // 见 4.6
  fun act(frame: Frame): Result = Actuator.run(this, frame)         // 见 4.5
  override fun onInterrupt() {}
  companion object { var instance: XmclawAccessibilityService? = null }
}
```

### 4.3 `Actuator`（执行动作）

分派 §2.3 规范命令（`data` 里的 `{"ui":...}` / `{"clipboard_cmd":...}`）：

```kotlin
object Actuator {
  // cmd = 解析自 data 的命令对象（含 ui 或 clipboard_cmd 字段）
  fun run(svc: AccessibilityService, cmd: Cmd): Result {
    cmd.clipboardCmd?.let { return clipboard(svc, it, cmd.text) }   // get/set_clipboard
    if (Blocklist.isWrite(cmd.ui) && Blocklist.blocked(currentPkg(svc)))
      return Result.fail("blocked: sensitive app ${currentPkg(svc)}")
    return when (cmd.ui) {
      "open_app"  -> launch(svc, cmd.packageName)
      "click"     -> clickTarget(svc, cmd.target)        // 选择器→节点 ACTION_CLICK，退 center 手势
      "tap"       -> gestureTap(svc, cmd.x, cmd.y)
      "input"     -> setText(svc, cmd.text, cmd.index)   // editable 节点 ACTION_SET_TEXT，原生中文
      "swipe"     -> gestureSwipe(svc, cmd.x1, cmd.y1, cmd.x2, cmd.y2, cmd.ms)
      "key_event" -> keyEvent(svc, cmd.key)              // BACK/HOME→performGlobalAction；其余注入
      "screenshot"-> ScreenCaptureService.capture()       // → obs.screenshot{url}
      "tree"      -> Result.tree(TreeReader.flatten(svc.rootInActiveWindow, cmd.clickableOnly))
      "notification" -> svc.performGlobalAction(GLOBAL_ACTION_NOTIFICATIONS).asResult()
      "long_press"-> longPress(svc, cmd)
      "wait"      -> waitFor(svc, cmd.event, cmd.target, cmd.timeoutMs)  // 轮询树 exists/gone
      "observe"   -> link.setObserve(cmd.on, cmd.onWindowChange, cmd.minIntervalMs).asResult()
      else        -> Result.fail("unknown ui ${cmd.ui}")
    }
  }
  private fun gestureTap(svc: AccessibilityService, x:Int, y:Int): Result {
    val p = Path().apply { moveTo(x.toFloat(), y.toFloat()) }
    val g = GestureDescription.Builder().addStroke(StrokeDescription(p, 0, 50)).build()
    return dispatch(svc, g)   // dispatchGesture + CountDownLatch 等回调
  }
  private fun keyEvent(svc: AccessibilityService, key: String) = when (key) {
    "KEYCODE_BACK" -> svc.performGlobalAction(GLOBAL_ACTION_BACK).asResult()
    "KEYCODE_HOME" -> svc.performGlobalAction(GLOBAL_ACTION_HOME).asResult()
    "KEYCODE_APP_SWITCH" -> svc.performGlobalAction(GLOBAL_ACTION_RECENTS).asResult()
    else -> injectKey(key)   // 其余 KEYCODE_* 经聚焦节点 / IME 注入
  }
}
```

> `clickTarget` 用 §2.3.1 选择器在当前树里找节点（res_id > text 精确 > desc > text 包含），
> 命中调 `ACTION_CLICK`，失败退 center 坐标手势；`wait` 轮询树直到 target `exists`/`gone` 或超时。

### 4.4 截图 `ScreenCaptureService`
- `MediaProjectionManager.createScreenCaptureIntent()` → Activity 一次性授权 → `MediaProjection` +
  `ImageReader` 抓一帧 → Bitmap → PNG → POST `/api/v2/uploads` → 回 URL → `obs.screenshot{url,w,h}`。
- 没授权投屏时，`perceive.screenshot` 降级回 `obs.tree`（仅树），并提示 agent。

### 4.5 `DaemonLink`（WS 客户端）
- OkHttp `WebSocket`；`onMessage` 解析帧 → 路由到无障碍服务/截图服务 → 把结果按 `req_id` 回帧。
- 断线指数退避重连；离线时缓存 `obs.event` 小队列，重连冲洗（对齐前端 lib/api 思路）。
- 配对令牌存 `EncryptedSharedPreferences`。

### 4.6 `TreeReader` + `NodeRef`（稳定 id）
- 递归 `rootInActiveWindow`，跳过不可见/空节点，输出扁平 `List<Node>`。
- `node_id` 方案：本帧内自增（n0,n1,…）+ 服务端**只在同一帧内**用 node_id 回点；跨帧动作用 center 坐标或
  res_id+text 重新定位（避免 id 失效）。在帧里同时给 `center`/`res_id` 兜底。

### 4.7 `CompanionActivity`（配对 + 权限引导）
- 输入/扫码 daemon 地址 + 配对码 → 存令牌 → 启动前台服务连 WS。
- 引导开启：无障碍服务（跳系统设置）、通知权限、（可选）投屏授权、后台保活。
- M3：内嵌任务/时间线视图（消费 daemon 的 `BehavioralEvent`，Mission Control 移动投影）。

---

## 5. 端到端时序样例：「打开设置，进 WLAN」

下行帧均为 `{"type":"cmd","req_id":..,"data":<命令>}`，下面只写 `data`：

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

## 6. 安全实现点（必须先于功能做实）

1. **`Blocklist`（手机端 + daemon 端双重）**：写动作（tap/text/global）前检查前台包名；命中银行/支付/证券/
   保险默认列表 → 拒绝并回 `blocked`。列表内置 + 可配。
2. **确认网关**：daemon `security.tool_guard` 对 `phone_text`、疑似支付/发送/删除动作插入审批；
   审批可在手机端（`user.approval`）或电脑端响应。
3. **配对/加密**：`pairing_token` + 局域网优先；公网必须 TLS。device_id 绑定，未配对拒连。
4. **脱敏**：`obs.tree`/截图进记忆前过 `security.redactor`；截图默认不长期留存（用完即弃或短 TTL）。
5. **可见可停**：常驻通知"XMclaw 正在控制本机"+ 一键断连；电脑端一键踢设备（FAILSAFE 等价）。

---

## 7. 构建 / 运行 / 调试

1. daemon：`config.json` 开 `tools.android_companion.enabled=true`；`xmclaw start`；记下配对码（`pairing_token`）。
2. App：Android Studio Run 装到手机 → 开无障碍服务 → 填 daemon 局域网地址 + 配对码 → 前台服务连上。
3. 联调：先用 `phone_ui_tree`/`phone_screenshot` 验证感知，再验证 `phone_tap`/`phone_text`。
4. 无 App 时：用 `providers/tool/android.py`（ADB 版，数据线）跑同名语义的工具先打通 daemon 侧逻辑。

---

## 8. 测试策略

- **daemon 单测**（必做，无真机）：DeviceRegistry 配对/超时；AndroidRemoteToolProvider 每工具发帧 +
  解析 + 无设备降级（mock DeviceConn）。登记到 `scripts/test_lanes.yaml` 的 `tools` lane。
- **协议契约测试**：一份 fixtures（样例帧）两端各跑一遍编解码，防 schema 漂移。
- **手机端**：无障碍服务/Actuator 用 Robolectric + 仪器测试（模拟器）；TreeReader 用离线 AccessibilityNodeInfo mock。
- **端到端冒烟**：模拟器 + daemon，跑 §5 时序脚本。

---

## 9. 里程碑 → 任务拆解

**M0 协议契约 + daemon 骨架**
- [ ] `docs/android_protocol_v1.md`（从 §2 抽出，两端引用）
- [ ] `DeviceRegistry` + `/device/v1/{id}` 路由 + 配对校验
- [ ] `test_v2_device_registry.py`（mock WS）

**M1 手眼最小闭环（按 §2.3 规范命令集）**
- [ ] App：Manifest/权限、`XmclawAccessibilityService`、`Actuator`(open_app/click/tap/input/swipe/key_event/tree/notification/wait/long_press)、剪贴板(get/set_clipboard)、`DaemonLink`、配对 Activity
- [ ] App：`ScreenCaptureService`（screenshot → uploads → url）
- [ ] daemon：`AndroidRemoteToolProvider`（phone_open_app/click/tap/input/swipe/key/screenshot/ui_tree/notification/wait/clipboard_get/set）+ factory 门控
- [ ] 端到端：§5 时序（打开设置→按元素点 WLAN→wait→截图）；`test_v2_android_remote_tools.py`

**M2 决策回路 + 中文输入 + 安全**
- [ ] AgentLoop 用树+图驱动多步任务（无新代码，验证回路）
- [ ] setText 中文验证；`Blocklist` 双端 + 确认网关接 `tool_guard`

**M3 反向控制（端 UI）**
- [ ] `user.message`/`user.approval` 上行接 AgentLoop/审批
- [ ] 手机端任务/时间线视图（Mission Control 移动投影）

**M4 跨设备 + 进化**
- [ ] 手机操作进统一记忆 / Honest-Grader
- [ ] 电脑+手机协同任务样例

每个里程碑**先看效果再扩**（Mission Control 教训）。

---

## 10. 风险 / 限制

- 无障碍服务被国产 ROM 杀后台/限权 → 前台服务 + 厂商保活引导；仍可能不稳，需实测各 ROM。
- `dispatchGesture` 在部分输入法/游戏/安全键盘上失效 → 优先节点动作，手势兜底。
- 投屏授权每次重启可能要重授（系统限制）→ 文档提示；纯树模式作降级。
- node_id 跨帧失效 → 用 center/res_id+text 重定位（§4.6）。
- 公网穿透/延迟（决策 2 若要公网）→ M1 不做，留 M4+。

---

## 11. 与既有代码的接点（避免重复造轮子）

- 鉴权：`pairing_token`（现成）。
- 工具 IR：`xmclaw.core.ir` 的 `ToolSpec/ToolCall/ToolResult`；附件 `metadata.attach_image[_url]`（现成渲染管线）。
- 安全：`security.tool_guard`、`security.redactor`（现成）。
- 错误透传：`utils/http_errors`（现成，刚做）。
- 事件：`core/bus/events.py` 加 `DEVICE_EVENT` 类型；UI 复用事件→条目映射。
- 上传：`/api/v2/uploads` + `_ensure_servable`（现成）。
- 回退：`providers/tool/android.py`（ADB 版，现成）。
