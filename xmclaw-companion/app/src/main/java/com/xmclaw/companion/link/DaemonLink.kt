package com.xmclaw.companion.link

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import java.util.concurrent.TimeUnit

/**
 * 与 XMclaw Daemon 的 OkHttp WebSocket 长连接。
 *
 * 协议契约（所有帧共用，见 docs/android_protocol_v1.md）：
 * {"v":1,"type":"...","req_id":"...","ts":...,"data":{...}}
 *
 * 端点: ws://<host>/device/v1/<device_id>?token=<pairing_token>
 */
class DaemonLink {

    private val client = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(15, TimeUnit.SECONDS)
        .build()

    @Volatile
    private var webSocket: WebSocket? = null

    /** 连接建立回调 */
    var onConnected: (() -> Unit)? = null
    /** 连接断开回调 */
    var onDisconnected: (() -> Unit)? = null
    /** 收到消息回调 */
    var onMessage: ((Frame) -> Unit)? = null

    /** 离线事件缓存队列（最多 20 条） */
    private val offlineQueue = ArrayDeque<Frame>(20)

    /** 指数退避重连计数器 */
    private var reconnectAttempt = 0
    private val maxReconnectDelayMs = 30_000L

    /**
     * 建立 WebSocket 连接。
     * @param baseUrl  Daemon 地址，如 "ws://192.168.1.5:8766"
     * @param token    配对令牌（pairing_token）
     * @param deviceId 设备唯一标识
     */
    fun connect(baseUrl: String, token: String, deviceId: String) {
        disconnect()
        val wsUrl = when {
            baseUrl.startsWith("http://") -> baseUrl.replaceFirst("http://", "ws://")
            baseUrl.startsWith("https://") -> baseUrl.replaceFirst("https://", "wss://")
            else -> baseUrl
        }

        val request = Request.Builder()
            .url("$wsUrl/device/v1/$deviceId?token=$token")
            .build()

        webSocket = client.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(ws: WebSocket, response: Response) {
                reconnectAttempt = 0
                onConnected?.invoke()
                // 发送 dev.hello 握手
                send(Frame.cmd(
                    type = "dev.hello",
                    data = buildJsonObject {
                        put("device_id", JsonPrimitive(deviceId))
                        put("name", JsonPrimitive("XMclaw Companion"))
                        put("model", JsonPrimitive(android.os.Build.MODEL))
                        put("android", JsonPrimitive(android.os.Build.VERSION.RELEASE))
                        put("app_ver", JsonPrimitive("1.0.0"))
                        put("perms", buildJsonObject {
                            put("accessibility", JsonPrimitive(true))
                            put("projection", JsonPrimitive(false))
                            put("notifications", JsonPrimitive(true))
                        })
                        put("screen", buildJsonObject {
                            put("w", JsonPrimitive(1080))  // TODO: 运行时读取
                            put("h", JsonPrimitive(2400))
                            put("density", JsonPrimitive(2.75))
                        })
                    }
                ))
                // 冲洗离线队列
                while (offlineQueue.isNotEmpty()) {
                    send(offlineQueue.removeFirst())
                }
            }

            override fun onMessage(ws: WebSocket, text: String) {
                try {
                    val frame = Frame.decode(text)
                    onMessage?.invoke(frame)
                } catch (_: Exception) {
                    // 忽略无法解析的非法帧
                }
            }

            override fun onClosing(ws: WebSocket, code: Int, reason: String) {
                ws.close(1000, null)
            }

            override fun onClosed(ws: WebSocket, code: Int, reason: String) {
                onDisconnected?.invoke()
                scheduleReconnect(baseUrl, token, deviceId)
            }

            override fun onFailure(ws: WebSocket, t: Throwable, response: Response?) {
                onDisconnected?.invoke()
                scheduleReconnect(baseUrl, token, deviceId)
            }
        })
    }

    /** 发送一帧；若未连接则缓存到离线队列 */
    fun send(frame: Frame): Boolean {
        val payload = frame.encode()
        val sent = webSocket?.send(payload) ?: false
        if (!sent) {
            synchronized(offlineQueue) {
                if (offlineQueue.size >= 20) offlineQueue.removeFirst()
                offlineQueue.addLast(frame)
            }
        }
        return sent
    }

    /** 主动断开 */
    fun disconnect() {
        webSocket?.close(1000, "client disconnect")
        webSocket = null
    }

    private fun scheduleReconnect(baseUrl: String, token: String, deviceId: String) {
        val delay = minOf(1000L * (1 shl reconnectAttempt), maxReconnectDelayMs)
        reconnectAttempt++
        android.os.Handler(android.os.Looper.getMainLooper()).postDelayed({
            connect(baseUrl, token, deviceId)
        }, delay)
    }
}
