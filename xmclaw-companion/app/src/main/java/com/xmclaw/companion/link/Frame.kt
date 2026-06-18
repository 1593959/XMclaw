package com.xmclaw.companion.link

import kotlinx.serialization.Serializable
import kotlinx.serialization.decodeFromString
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

private val json = Json {
    ignoreUnknownKeys = true
    encodeDefaults = true
}

/**
 * XMclaw 协议帧数据类。
 * 所有通过 WebSocket 与 Daemon 交换的消息都封装为此格式。
 *
 * 统一帧格式：{"v":1,"type":"...","req_id":"...","ts":...,"data":{...}}
 */
@Serializable
data class Frame(
    /** 协议版本，默认 1 */
    val v: Int = 1,
    /** 帧类型：dev.hello | obs.* | act.result | user.* | cmd */
    val type: String,
    /** 请求 ID，用于 cmd/result 配对；不需要回执时为 null */
    val req_id: String? = null,
    /** 时间戳，epoch 秒（float） */
    val ts: Double = System.currentTimeMillis() / 1000.0,
    /** 载荷数据 */
    val data: JsonObject
) {
    /** 编码为 JSON 字符串 */
    fun encode(): String = json.encodeToString(this)

    companion object {
        /** 从 JSON 字符串解码 */
        fun decode(text: String): Frame = json.decodeFromString(text)

        /**
         * 构造事件帧（设备 → Daemon）。
         * @param type 事件类型，如 obs.tree、obs.screenshot、obs.event
         * @param data 事件载荷
         * @param reqId 可选请求 ID（应答时使用）
         */
        fun event(type: String, data: JsonObject, reqId: String? = null): Frame =
            Frame(type = type, req_id = reqId, data = data)

        /**
         * 构造命令帧（Daemon → 设备）。
         * @param type 命令类型，如 cmd
         * @param data 命令参数（{"ui":...} / {"clipboard_cmd":...}）
         * @param reqId 请求 ID，必须提供
         */
        fun cmd(type: String, data: JsonObject, reqId: String? = null): Frame =
            Frame(type = type, req_id = reqId, data = data)

        /**
         * 构造结果帧（设备 → Daemon，响应 cmd）。
         * @param reqId 对应命令的 req_id
         * @param ok 是否成功
         * @param data 可选返回数据
         * @param error 可选错误信息
         */
        fun result(reqId: String, ok: Boolean, data: JsonObject? = null, error: String? = null): Frame =
            Frame(
                type = "act.result",
                req_id = reqId,
                data = buildJsonObject {
                    put("ok", ok)
                    if (error != null) put("error", error)
                    if (data != null) data.forEach { (k, value) -> put(k, value) }
                }
            )
    }
}
