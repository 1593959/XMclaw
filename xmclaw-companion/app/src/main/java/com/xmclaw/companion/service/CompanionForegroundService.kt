package com.xmclaw.companion.service

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import android.provider.Settings
import android.util.Log
import androidx.core.app.NotificationCompat
import com.xmclaw.companion.CompanionApp
import com.xmclaw.companion.link.Frame
import com.xmclaw.companion.ui.CompanionActivity
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put

/**
 * XMclaw 常驻前台服务 —— 保持 WebSocket 连接并协调各子服务。
 *
 * 通知标题: "XMclaw 正在控制本机"
 * 通知点击 → 断开连接
 */
class CompanionForegroundService : Service() {

    companion object {
        private const val TAG = "CompanionFg"
        private const val CHANNEL_ID = "xmclaw_companion"
        private const val NOTIFICATION_ID = 1
        private const val ACTION_DISCONNECT = "com.xmclaw.companion.DISCONNECT"

        /** 启动前台服务 */
        fun start(context: Context, daemonUrl: String, token: String) {
            val intent = Intent(context, CompanionForegroundService::class.java).apply {
                putExtra("daemon_url", daemonUrl)
                putExtra("token", token)
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }

        /** 停止前台服务 */
        fun stop(context: Context) {
            context.stopService(Intent(context, CompanionForegroundService::class.java))
        }

        /**
         * 发送用户主动消息给 Agent。
         * @param text 用户文本
         * @param imageUrls 可选图片 URL 列表
         */
        fun sendUserMessage(text: String, imageUrls: List<String>? = null) {
            val data = buildJsonObject {
                put("text", JsonPrimitive(text))
                if (imageUrls != null) {
                    put("images", JsonArray(imageUrls.map { JsonPrimitive(it) }))
                }
            }
            val frame = Frame.event(
                type = "user.message",
                data = data
            )
            CompanionApp.daemonLink.send(frame)
        }

        /**
         * 发送用户审批响应。
         * @param requestId 审批请求 ID
         * @param decision 决策：allow / allow_always / deny
         */
        fun sendApproval(requestId: String, decision: String) {
            val frame = Frame.event(
                type = "user.approval",
                data = buildJsonObject {
                    put("request_id", JsonPrimitive(requestId))
                    put("decision", JsonPrimitive(decision))
                }
            )
            CompanionApp.daemonLink.send(frame)
        }
    }

    private val serviceScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var daemonUrl: String = ""
    private var token: String = ""
    private var deviceId: String = ""

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_DISCONNECT) {
            disconnect()
            stopSelf(startId)
            return START_NOT_STICKY
        }

        daemonUrl = intent?.getStringExtra("daemon_url") ?: ""
        token = intent?.getStringExtra("token") ?: ""
        deviceId = Settings.Secure.getString(contentResolver, Settings.Secure.ANDROID_ID)
            ?: "xmclaw_${System.currentTimeMillis()}"

        if (daemonUrl.isBlank() || token.isBlank()) {
            stopSelf(startId)
            return START_NOT_STICKY
        }

        startForeground(NOTIFICATION_ID, buildNotification())
        connectDaemon()

        return START_STICKY
    }

    private fun connectDaemon() {
        CompanionApp.daemonLink.onConnected = {
            Log.i(TAG, "Daemon 已连接")
            updateNotification("已连接")
        }
        CompanionApp.daemonLink.onDisconnected = {
            Log.w(TAG, "Daemon 断开")
            updateNotification("已断开 — 5s 后重连")
            serviceScope.launch {
                delay(5000)
                if (serviceScope.isActive) connectDaemon()
            }
        }
        CompanionApp.daemonLink.onMessage = { frame ->
            handleFrame(frame)
        }
        CompanionApp.daemonLink.connect(daemonUrl, token, deviceId)
    }

    /**
     * 处理 Daemon 下发的帧。
     */
    private fun handleFrame(frame: Frame) {
        when (frame.type) {
            "cmd" -> {
                val ui = frame.data["ui"]?.jsonPrimitive?.contentOrNull
                // 截图命令需要 daemonUrl 和 token，在 Service 层直接处理
                if (ui == "screenshot") {
                    serviceScope.launch {
                        val url = ScreenCaptureService.instance?.captureAndUpload(daemonUrl, token)
                        val response = Frame.event(
                            type = "obs.screenshot",
                            reqId = frame.req_id,
                            data = buildJsonObject {
                                put("url", JsonPrimitive(url ?: ""))
                                put("success", JsonPrimitive(url != null))
                            }
                        )
                        CompanionApp.daemonLink.send(response)
                    }
                } else {
                    // 其余 UI 操作与剪贴板命令交给无障碍服务处理
                    CompanionApp.accessibilityService?.act(frame)
                }
            }
            "dev.welcome" -> {
                val version = frame.data["version"]?.jsonPrimitive?.contentOrNull
                val heartbeat = frame.data["heartbeat"]?.jsonPrimitive?.intOrNull
                Log.i(TAG, "Daemon 欢迎帧: version=$version, heartbeat=${heartbeat}s")
            }
            else -> Log.d(TAG, "收到未知帧: ${frame.type}")
        }
    }

    private fun disconnect() {
        CompanionApp.daemonLink.disconnect()
        stopService(Intent(this, ScreenCaptureService::class.java))
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                "XMclaw 守护服务",
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = "保持与 XMclaw Daemon 的连接"
            }
            val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            manager.createNotificationChannel(channel)
        }
    }

    private fun buildNotification(): android.app.Notification {
        val disconnectIntent = Intent(this, CompanionForegroundService::class.java).apply {
            action = ACTION_DISCONNECT
        }
        val disconnectPending = PendingIntent.getService(
            this, 0, disconnectIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("XMclaw 正在控制本机")
            .setContentText("守护进程已连接")
            .setSmallIcon(android.R.drawable.ic_menu_info_details)
            .setContentIntent(disconnectPending)
            .setOngoing(true)
            .build()
    }

    private fun updateNotification(text: String) {
        val notification = NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("XMclaw 正在控制本机")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.ic_menu_info_details)
            .setOngoing(true)
            .build()
        val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        manager.notify(NOTIFICATION_ID, notification)
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        disconnect()
        serviceScope.cancel()
        super.onDestroy()
    }
}
