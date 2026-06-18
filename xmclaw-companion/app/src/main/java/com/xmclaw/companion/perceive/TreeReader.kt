package com.xmclaw.companion.perceive

import android.accessibilityservice.AccessibilityService
import android.graphics.Rect
import android.view.accessibility.AccessibilityNodeInfo
import kotlinx.serialization.Serializable
import kotlinx.serialization.Transient

/**
 * 可序列化的 UI 节点。
 * 表示屏幕上的一个可交互元素，包含物理像素坐标。
 * rawNode 不参与序列化，仅供本地 Actuator 直接操作。
 */
@Serializable
data class Node(
    /** 帧内唯一编号，如 n0, n1... */
    val id: String,
    /** 文本内容 */
    val text: String?,
    /** 资源 ID，如 com.example:id/btn_ok */
    val res_id: String?,
    /** 内容描述（accessibility desc） */
    val desc: String?,
    /** 类名，如 android.widget.Button */
    val cls: String?,
    /** 是否可点击（包含 isFocusable） */
    val clickable: Boolean,
    /** 是否可编辑 */
    val editable: Boolean,
    /** 屏幕物理像素边界 [x1, y1, x2, y2] */
    val bounds: List<Int>,
    /** 中心点 [cx, cy] */
    val center: List<Int>,
    /** 原始节点引用，不参与序列化 */
    @Transient
    val rawNode: AccessibilityNodeInfo? = null
)

/**
 * 无障碍节点树读取器。
 * 递归遍历当前窗口根节点，输出扁平化的节点列表。
 */
object TreeReader {

    /**
     * 读取当前活跃窗口的节点树。
     * @param service 无障碍服务实例
     * @param clickableOnly 是否仅返回可点击节点
     * @return 扁平节点列表
     */
    fun read(service: AccessibilityService, clickableOnly: Boolean = false): List<Node> {
        val root = service.rootInActiveWindow ?: return emptyList()
        val nodes = mutableListOf<Node>()
        var counter = 0

        fun traverse(node: AccessibilityNodeInfo) {
            // 跳过不可见节点
            if (!node.isVisibleToUser) return

            // 跳过空节点（无文本、无描述、无 ID、无类名、无子节点）
            val isEmpty = node.text.isNullOrBlank()
                    && node.contentDescription.isNullOrBlank()
                    && node.viewIdResourceName.isNullOrBlank()
                    && node.className.isNullOrBlank()
                    && node.childCount == 0
            if (isEmpty) return

            val rect = Rect()
            node.getBoundsInScreen(rect)

            val clickable = node.isClickable || node.isFocusable
            val editable = node.isEditable

            if (clickableOnly && !clickable) {
                // 不可点击节点仅递归，不加入结果
            } else {
                val n = Node(
                    id = "n${counter++}",
                    text = node.text?.toString(),
                    res_id = node.viewIdResourceName,
                    desc = node.contentDescription?.toString(),
                    cls = node.className?.toString(),
                    clickable = clickable,
                    editable = editable,
                    bounds = listOf(rect.left, rect.top, rect.right, rect.bottom),
                    center = listOf(rect.centerX(), rect.centerY()),
                    rawNode = node
                )
                nodes.add(n)
            }

            // 递归子节点
            for (i in 0 until node.childCount) {
                node.getChild(i)?.let { traverse(it) }
            }
        }

        traverse(root)
        return nodes
    }
}
