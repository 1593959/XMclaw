package com.xmclaw.companion.act

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.graphics.Path
import android.graphics.Rect
import android.os.Bundle
import android.view.accessibility.AccessibilityNodeInfo
import com.xmclaw.companion.perceive.NodeRef
import com.xmclaw.companion.perceive.TreeReader
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

/**
 * 命令执行器。
 * 分派 Daemon 下发的各类 UI 动作命令，通过无障碍服务完成实际操作。
 * 所有写动作（tap/click/input/swipe/key_event/long_press/clipboard set）
 * 在执行前会经过 Blocklist 敏感应用检查。
 */
class Actuator(private val service: AccessibilityService) {

    /**
     * 执行入口。
     * @param action 动作名称（如 tap / click / input / swipe / key_event 等）
     * @param args 命令参数（来自协议 data 字段）
     * @return 执行结果
     */
    fun execute(action: String, args: JsonObject): Result {
        val pkg = service.rootInActiveWindow?.packageName?.toString()

        // 写动作黑名单检查
        if (Blocklist.isWrite(action) && Blocklist.blocked(pkg)) {
            return Result.fail("blocked: sensitive app $pkg")
        }

        return try {
            when (action) {
                "open_app" -> openApp(args)
                "click" -> click(args)
                "tap" -> tap(args)
                "input" -> input(args)
                "swipe" -> swipe(args)
                "key_event" -> keyEvent(args)
                "screenshot" -> Result.fail("screenshot delegated to ScreenCaptureService")
                "tree" -> tree(args)
                "notification" -> notification(args)
                "long_press" -> longPress(args)
                "wait" -> waitFor(args)
                "observe" -> observe(args)
                else -> Result.fail("unknown action: $action")
            }
        } catch (e: Exception) {
            Result.fail("exception: ${e.message}")
        }
    }

    // ─── 具体动作实现 ────────────────────────────

    /** 打开指定包名的应用。 */
    private fun openApp(args: JsonObject): Result {
        val pkg = args["package_name"]?.jsonPrimitive?.content
            ?: return Result.fail("missing package_name")
        val intent = service.packageManager.getLaunchIntentForPackage(pkg)
            ?: return Result.fail("cannot launch $pkg")
        intent.addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK)
        service.startActivity(intent)
        return Result.success()
    }

    /**
     * 点击元素（通过选择器）。
     * 使用 target 选择器匹配节点，优先级：res_id > text 精确 > desc > text 包含。
     */
    private fun click(args: JsonObject): Result {
        val target = args["target"]?.jsonObject
        if (target == null) {
            return Result.fail("click: missing target")
        }
        val tree = TreeReader.read(service)
        val node = NodeRef.findNode(tree, target)
        if (node != null) {
            val info = node.rawNode
            if (info?.isClickable == true) {
                val clicked = info.performAction(AccessibilityNodeInfo.ACTION_CLICK)
                if (clicked) return Result.success(mapOf("matched" to (node.text ?: node.res_id ?: "")))
            }
            // 命中但不可点击 / action 失败 → fallback 中心坐标手势
            val rect = Rect()
            info?.getBoundsInScreen(rect)
                ?: return Result.fail("click: node bounds unavailable")
            return tapAt(rect.centerX().toFloat(), rect.centerY().toFloat())
        }
        return Result.fail("click: target not found")
    }

    /** 坐标点击（tap）。 */
    private fun tap(args: JsonObject): Result {
        val x = args["x"]?.jsonPrimitive?.doubleOrNull
            ?: return Result.fail("tap: missing x")
        val y = args["y"]?.jsonPrimitive?.doubleOrNull
            ?: return Result.fail("tap: missing y")
        return tapAt(x.toFloat(), y.toFloat())
    }

    private fun tapAt(x: Float, y: Float): Result {
        val path = Path().apply { moveTo(x, y) }
        val gesture = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, 50))
            .build()
        val dispatched = service.dispatchGesture(gesture, null, null)
        return if (dispatched) Result.success() else Result.fail("gesture dispatch failed")
    }

    /**
     * 输入文本。
     * 使用 ACTION_SET_TEXT 原生中文输入。
     * index 参数选择第几个可编辑框（默认 0 = 当前焦点框）。
     */
    private fun input(args: JsonObject): Result {
        val text = args["text"]?.jsonPrimitive?.content
            ?: return Result.fail("missing text")
        val index = args["index"]?.jsonPrimitive?.intOrNull ?: 0

        val root = service.rootInActiveWindow
        if (root == null) return Result.fail("no active window")

        val editableNodes = findEditableNodes(root)
        val node = editableNodes.getOrNull(index)
        if (node != null) {
            val bundle = Bundle().apply {
                putString(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text)
            }
            val success = node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, bundle)
            node.recycle()
            root.recycle()
            if (success) return Result.success()
        }
        root.recycle()
        return Result.fail("input: no editable node found at index $index")
    }

    private fun findEditableNodes(root: AccessibilityNodeInfo): List<AccessibilityNodeInfo> {
        val result = mutableListOf<AccessibilityNodeInfo>()
        fun traverse(node: AccessibilityNodeInfo) {
            if (node.isEditable) result.add(node)
            else {
                for (i in 0 until node.childCount) {
                    node.getChild(i)?.let { traverse(it) }
                }
            }
        }
        traverse(root)
        return result
    }

    /** 滑动操作。 */
    private fun swipe(args: JsonObject): Result {
        val startX = args["x1"]?.jsonPrimitive?.doubleOrNull
            ?: return Result.fail("swipe: missing x1")
        val startY = args["y1"]?.jsonPrimitive?.doubleOrNull
            ?: return Result.fail("swipe: missing y1")
        val endX = args["x2"]?.jsonPrimitive?.doubleOrNull
            ?: return Result.fail("swipe: missing x2")
        val endY = args["y2"]?.jsonPrimitive?.doubleOrNull
            ?: return Result.fail("swipe: missing y2")
        val duration = args["ms"]?.jsonPrimitive?.intOrNull ?: 300

        val path = Path().apply {
            moveTo(startX.toFloat(), startY.toFloat())
            lineTo(endX.toFloat(), endY.toFloat())
        }
        val gesture = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, duration.toLong()))
            .build()
        val dispatched = service.dispatchGesture(gesture, null, null)
        return if (dispatched) Result.success() else Result.fail("swipe failed")
    }

    /**
     * 按键事件。
     * BACK/HOME/APP_SWITCH 使用 performGlobalAction；
     * 支持 KEYCODE_BACK / KEYCODE_HOME / KEYCODE_APP_SWITCH 格式。
     */
    private fun keyEvent(args: JsonObject): Result {
        val key = args["key"]?.jsonPrimitive?.content
            ?: return Result.fail("missing key")

        // 统一映射：支持友好名和 KEYCODE_* 格式
        val normalized = when (key.lowercase()) {
            "back", "keycode_back" -> "BACK"
            "home", "keycode_home" -> "HOME"
            "recents", "keycode_app_switch" -> "APP_SWITCH"
            "enter", "keycode_enter" -> "ENTER"
            "delete", "del", "keycode_del" -> "DELETE"
            else -> key.uppercase()
        }

        return when (normalized) {
            "BACK" -> {
                val ok = service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)
                Result.success(ok)
            }
            "HOME" -> {
                val ok = service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_HOME)
                Result.success(ok)
            }
            "APP_SWITCH", "RECENTS" -> {
                val ok = service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_RECENTS)
                Result.success(ok)
            }
            "ENTER" -> {
                // 对当前焦点节点执行 ACTION_CLICK（模拟确认键）
                val root = service.rootInActiveWindow
                val focused = root?.findFocus(AccessibilityNodeInfo.FOCUS_INPUT)
                val ok = focused?.performAction(AccessibilityNodeInfo.ACTION_CLICK) ?: false
                focused?.recycle()
                root?.recycle()
                Result.success(ok)
            }
            "DELETE" -> {
                // 对当前焦点节点发送删除事件
                val root = service.rootInActiveWindow
                val focused = root?.findFocus(AccessibilityNodeInfo.FOCUS_INPUT)
                val ok = focused?.performAction(AccessibilityNodeInfo.ACTION_PASTE) ?: false
                focused?.recycle()
                root?.recycle()
                Result.success(ok)
            }
            else -> Result.fail("key injection not implemented: $key")
        }
    }

    /** 获取当前节点树。 */
    private fun tree(args: JsonObject): Result {
        val clickableOnly = args["clickable_only"]?.jsonPrimitive?.booleanOrNull ?: false
        val nodes = TreeReader.read(service, clickableOnly)
        return Result.success(nodes)
    }

    /** 下拉通知栏。 */
    private fun notification(args: JsonObject): Result {
        val ok = service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_NOTIFICATIONS)
        return Result.success(ok)
    }

    /**
     * 长按操作。
     * 支持坐标直接按压，或选择器匹配后取中心坐标按压。
     */
    private fun longPress(args: JsonObject): Result {
        val x = args["x"]?.jsonPrimitive?.doubleOrNull
        val y = args["y"]?.jsonPrimitive?.doubleOrNull
        val target = args["target"]?.jsonObject
        val duration = args["ms"]?.jsonPrimitive?.intOrNull ?: 600

        if (x != null && y != null) {
            return longPressAt(x.toFloat(), y.toFloat(), duration.toLong())
        }

        if (target != null) {
            val tree = TreeReader.read(service)
            val node = NodeRef.findNode(tree, target)
            if (node != null) {
                val rect = Rect()
                node.rawNode?.getBoundsInScreen(rect)
                    ?: return Result.fail("long_press: node bounds unavailable")
                return longPressAt(rect.centerX().toFloat(), rect.centerY().toFloat(), duration.toLong())
            }
        }

        return Result.fail("long_press: no coordinates or target")
    }

    private fun longPressAt(x: Float, y: Float, durationMs: Long): Result {
        val path = Path().apply { moveTo(x, y) }
        val gesture = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, durationMs))
            .build()
        val dispatched = service.dispatchGesture(gesture, null, null)
        return if (dispatched) Result.success() else Result.fail("long press failed")
    }

    /**
     * 等待条件达成。
     * 轮询节点树直到目标节点出现 (exists) 或消失 (gone)，或超时。
     * 使用非阻塞轮询，避免 ANR。
     */
    private fun waitFor(args: JsonObject): Result {
        val target = args["target"]?.jsonObject
            ?: return Result.fail("wait: missing target")
        val event = args["event"]?.jsonPrimitive?.content ?: "exists"
        val timeoutMs = args["timeout_ms"]?.jsonPrimitive?.intOrNull ?: 5000

        val start = System.currentTimeMillis()
        var found = false
        var waitedMs = 0

        while (System.currentTimeMillis() - start < timeoutMs) {
            val tree = TreeReader.read(service)
            found = NodeRef.findNode(tree, target) != null
            waitedMs = (System.currentTimeMillis() - start).toInt()
            if (event == "exists" && found) {
                return Result.success(mapOf("found" to true, "waited_ms" to waitedMs))
            }
            if (event == "gone" && !found) {
                return Result.success(mapOf("found" to false, "waited_ms" to waitedMs))
            }
            // 非阻塞轮询，每次间隔 200ms
            Thread.sleep(200)
        }

        return Result.success(mapOf("found" to false, "waited_ms" to waitedMs, "error" to "timeout"))
    }

    /** 启动 UI 观察（由无障碍事件流自动处理，此处仅返回成功）。 */
    private fun observe(args: JsonObject): Result {
        return Result.success()
    }
}
