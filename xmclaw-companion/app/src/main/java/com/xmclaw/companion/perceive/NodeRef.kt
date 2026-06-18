package com.xmclaw.companion.perceive

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonPrimitive

/**
 * 节点选择器匹配器。
 * 提供基于多种属性的节点选择逻辑，用于 Actuator 定位目标节点。
 */
object NodeRef {

    /**
     * 在给定节点列表中查找匹配选择器的节点。
     * 匹配优先级：res_id > text 精确匹配 > desc 精确匹配 > text 包含匹配
     *
     * @param nodes 节点列表（通常来自 TreeReader.read()）
     * @param selector 选择器 JsonObject，支持字段：
     *   - res_id: 资源 ID 精确匹配
     *   - text: 文本精确匹配
     *   - desc: 描述精确匹配
     *   - text_contains: 文本包含匹配
     * @return 首个匹配的 Node，未命中返回 null
     */
    fun findNode(nodes: List<Node>, selector: JsonObject): Node? {
        val resId = selector["res_id"]?.jsonPrimitive?.content
        val textExact = selector["text"]?.jsonPrimitive?.content
        val desc = selector["desc"]?.jsonPrimitive?.content
        val textContains = selector["text_contains"]?.jsonPrimitive?.content

        // 优先级 1: res_id 精确匹配
        if (resId != null) {
            nodes.find { it.res_id == resId }?.let { return it }
        }

        // 优先级 2: text 精确匹配
        if (textExact != null) {
            nodes.find { it.text == textExact }?.let { return it }
        }

        // 优先级 3: desc 精确匹配
        if (desc != null) {
            nodes.find { it.desc == desc }?.let { return it }
        }

        // 优先级 4: text 包含匹配
        if (textContains != null) {
            nodes.find { it.text?.contains(textContains) == true }?.let { return it }
        }

        return null
    }
}
