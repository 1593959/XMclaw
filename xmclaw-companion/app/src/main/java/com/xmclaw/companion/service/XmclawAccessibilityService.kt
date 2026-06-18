package com.xmclaw.companion.service

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.graphics.Path
import android.graphics.Rect
import android.os.Bundle
import android.util.Log
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import com.xmclaw.companion.CompanionApp
import com.xmclaw.companion.act.Actuator
import com.xmclaw.companion.act.Result
import com.xmclaw.companion.link.Frame
import com.xmclaw.companion.perceive.Node
import com.xmclaw.companion.ui.CompanionActivity
import kotlinx.serialization.builtins.ListSerializer
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.floatOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * XMclaw 无障碍服务 —— 设备的"眼睛"与"双手"。
 * 通过 Accessibility API 读取界面树、执行点击/输入/手势操作，
 * 并将窗口变化事件通过 DaemonLink 按新协议格式上报。
 */
class XmclawAccessibilityService : AccessibilityService() {

    companion object {
        private const val TAG = "XmclawA11y"

        /** 单例引用，供外部快速访问 */
        @Volatile
        var instance: XmclawAccessibilityService? = null
            private set
    }

    private val json = Json {
        ignoreUnknownKeys = true
        encodeDefaults = true
    }

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
        CompanionApp.accessibilityService = this
        Log.i(TAG, "无障碍服务已连接")
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent) {
        when (event.eventType) {
            AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED -> {
                val pkg = event.packageName?.toString() ?: "unknown"
                val frame = Frame.event(
                    type = "obs.event",
                    data = buildJsonObject {
                        put("kind", JsonPrimitive("window_changed"))
                        put("pkg", JsonPrimitive(pkg))
                        put("activity", JsonPrimitive(event.className?.toString() ?: ""))
                    }
                )
                CompanionApp.daemonLink.send(frame)
            }
            AccessibilityEvent.TYPE_NOTIFICATION_STATE_CHANGED -> {
                val pkg = event.packageName?.toString() ?: "unknown"
                val frame = Frame.event(
                    type = "obs.event",
                    data = buildJsonObject {
                        put("kind", JsonPrimitive("notification"))
                        put("pkg", JsonPrimitive(pkg))
                        put("text", JsonPrimitive(event.text?.joinToString() ?: ""))
                    }
                )
                CompanionApp.daemonLink.send(frame)
            }
            AccessibilityEvent.TYPE_ANNOUNCEMENT -> {
                val pkg = event.packageName?.toString() ?: "unknown"
                val frame = Frame.event(
                    type = "obs.event",
                    data = buildJsonObject {
                        put("kind", JsonPrimitive("toast"))
                        put("pkg", JsonPrimitive(pkg))
                        put("text", JsonPrimitive(event.text?.joinToString() ?: ""))
                    }
                )
                CompanionApp.daemonLink.send(frame)
            }
            else -> {
                // 其他事件暂不上报
            }
        }
    }

    override fun onInterrupt() {
        Log.w(TAG, "无障碍服务被中断")
    }

    override fun onDestroy() {
        instance = null
        CompanionApp.accessibilityService = null
        super.onDestroy()
    }

    /**
     * 读取当前窗口的 Accessibility 树。
     * @param clickableOnly 为 true 时只返回可交互节点。
     * @return 节点 JSON 数组。
     */
    fun readTree(clickableOnly: Boolean = false): JsonArray {
        val root = rootInActiveWindow ?: return JsonArray(emptyList())
        val nodes = mutableListOf<JsonObject>()
        traverse(root, nodes, clickableOnly)
        root.recycle()
        return JsonArray(nodes)
    }

    private fun traverse(
        node: AccessibilityNodeInfo,
        out: MutableList<JsonObject>,
        clickableOnly: Boolean
    ) {
        if (!clickableOnly || node.isClickable || node.isEditable || node.isScrollable) {
            val rect = Rect()
            node.getBoundsInScreen(rect)
            val json = buildJsonObject {
                put("id", JsonPrimitive(node.viewIdResourceName ?: ""))
                put("class", JsonPrimitive(node.className?.toString() ?: ""))
                put("package", JsonPrimitive(node.packageName?.toString() ?: ""))
                put("text", JsonPrimitive(node.text?.toString() ?: ""))
                put("desc", JsonPrimitive(node.contentDescription?.toString() ?: ""))
                put("clickable", JsonPrimitive(node.isClickable))
                put("editable", JsonPrimitive(node.isEditable))
                put("scrollable", JsonPrimitive(node.isScrollable))
                put("focused", JsonPrimitive(node.isFocused))
                put("bounds", buildJsonObject {
                    put("left", JsonPrimitive(rect.left))
                    put("top", JsonPrimitive(rect.top))
                    put("right", JsonPrimitive(rect.right))
                    put("bottom", JsonPrimitive(rect.bottom))
                })
            }
            out.add(json)
        }
        for (i in 0 until node.childCount) {
            node.getChild(i)?.let { child ->
                traverse(child, out, clickableOnly)
                child.recycle()
            }
        }
    }

    /**
     * 执行一帧动作指令。
     * 解析 data 中的 ui 或 clipboard_cmd 字段，分别交给 Actuator 或本地剪贴板处理。
     * 执行后自动发送 act.result 响应。
     * @param frame 动作帧，data 中必须包含 ui 或 clipboard_cmd 字段。
     */
    fun act(frame: Frame) {
        val data = frame.data
        val reqId = frame.req_id

        val result: Result = when {
            data["ui"] != null -> {
                val ui = data["ui"]?.jsonPrimitive?.contentOrNull ?: ""
                val actuator = Actuator(this)
                actuator.execute(ui, data)
            }
            data["clipboard_cmd"] != null -> {
                val cmd = data["clipboard_cmd"]?.jsonPrimitive?.contentOrNull ?: ""
                handleClipboard(cmd, data)
            }
            else -> {
                Result.fail("unknown cmd: data 中缺少 'ui' 或 'clipboard_cmd' 字段")
            }
        }

        // 更新 Mission Control 列表（通知 UI 刷新）
        val time = SimpleDateFormat("HH:mm:ss", Locale.getDefault()).format(Date())
        val uiName = data["ui"]?.jsonPrimitive?.contentOrNull
            ?: data["clipboard_cmd"]?.jsonPrimitive?.contentOrNull
            ?: "unknown"
        val missionItem = CompanionActivity.MissionItem(
            time = time,
            type = uiName,
            status = if (result.ok) "成功" else "失败"
        )
        CompanionActivity.onMissionUpdate?.invoke(missionItem)

        // 构建 act.result 响应数据，将 Actuator 的 Result.data 转换为 JsonObject
        val resultData = buildJsonObject {
            if (result.data != null) {
                when (val d = result.data) {
                    is List<*> -> {
                        // 树形结果（List<Node>）序列化为 {"nodes": [...], "pkg": "...", "activity": "..."}
                        val nodes = d.filterIsInstance<Node>()
                        if (nodes.isNotEmpty()) {
                            val arr = json.encodeToJsonElement(ListSerializer(Node.serializer()), nodes)
                            put("nodes", arr)
                            val root = rootInActiveWindow
                            put("pkg", JsonPrimitive(root?.packageName?.toString() ?: ""))
                            put("activity", JsonPrimitive(root?.className?.toString() ?: ""))
                            root?.recycle()
                        }
                    }
                    is Map<*, *> -> {
                        d.forEach { (k, v) ->
                            put(k.toString(), JsonPrimitive(v.toString()))
                        }
                    }
                    is Boolean -> put("value", JsonPrimitive(d))
                    else -> put("result", JsonPrimitive(d.toString()))
                }
            }
        }

        val response = Frame.result(
            reqId = reqId ?: "",
            ok = result.ok,
            data = if (result.data != null) resultData else null,
            error = result.error
        )
        CompanionApp.daemonLink.send(response)
    }

    /**
     * 处理剪贴板命令。
     * @param cmd 命令类型：get / set
     * @param data 命令参数，set 时需提供 text 字段
     */
    private fun handleClipboard(cmd: String, data: JsonObject): Result {
        val clipboard = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
        return when (cmd) {
            "get_clipboard" -> {
                val text = clipboard.primaryClip?.getItemAt(0)?.text?.toString() ?: ""
                Result.success(mapOf("text" to text))
            }
            "set_clipboard" -> {
                val text = data["text"]?.jsonPrimitive?.contentOrNull
                    ?: return Result.fail("clipboard set: 缺少 text 字段")
                val clip = ClipData.newPlainText("xmclaw", text)
                clipboard.setPrimaryClip(clip)
                Result.success()
            }
            else -> Result.fail("unknown clipboard_cmd: $cmd")
        }
    }

    private fun performClick(x: Int, y: Int) {
        val path = Path().apply { moveTo(x.toFloat(), y.toFloat()) }
        val gesture = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, 100))
            .build()
        dispatchGesture(gesture, null, null)
    }

    private fun performLongClick(x: Int, y: Int) {
        val path = Path().apply { moveTo(x.toFloat(), y.toFloat()) }
        val gesture = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, 800))
            .build()
        dispatchGesture(gesture, null, null)
    }

    private fun performSetText(x: Int, y: Int, text: String) {
        val root = rootInActiveWindow ?: return
        val node = findNodeAt(root, x, y) { it.isEditable }
        if (node != null) {
            val args = Bundle().apply {
                putString(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text)
            }
            node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)
            node.recycle()
        } else {
            Log.w(TAG, "未找到可编辑节点 at ($x, $y)")
        }
        root.recycle()
    }

    private fun performGesture(points: JsonArray) {
        if (points.size < 2) return
        val path = Path()
        points.forEachIndexed { index, element ->
            val px = element.jsonObject["x"]?.jsonPrimitive?.floatOrNull ?: 0f
            val py = element.jsonObject["y"]?.jsonPrimitive?.floatOrNull ?: 0f
            if (index == 0) path.moveTo(px, py) else path.lineTo(px, py)
        }
        val gesture = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, 500))
            .build()
        dispatchGesture(gesture, null, null)
    }

    private fun findNodeAt(
        root: AccessibilityNodeInfo,
        x: Int, y: Int,
        predicate: (AccessibilityNodeInfo) -> Boolean
    ): AccessibilityNodeInfo? {
        val rect = Rect()
        root.getBoundsInScreen(rect)
        if (rect.contains(x, y) && predicate(root)) return root
        for (i in 0 until root.childCount) {
            val child = root.getChild(i) ?: continue
            val found = findNodeAt(child, x, y, predicate)
            if (found != null) {
                return found
            }
            child.recycle()
        }
        return null
    }
}
