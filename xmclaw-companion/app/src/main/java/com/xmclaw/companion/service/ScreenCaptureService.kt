package com.xmclaw.companion.service

import android.app.Activity
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.PixelFormat
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Handler
import android.os.HandlerThread
import android.os.IBinder
import android.util.DisplayMetrics
import android.util.Log
import android.view.WindowManager
import androidx.core.app.NotificationCompat
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.MediaType.Companion.toMediaType
import java.io.ByteArrayOutputStream

/**
 * 屏幕抓取服务 —— 使用 MediaProjection 截图并上传。
 *
 * 启动方式：
 * 1. Activity 通过 MediaProjectionManager 获取授权 Intent
 * 2. 授权后将 resultCode 与 data Intent 传入本 Service
 * 3. 本 Service 启动前台并持有 MediaProjection，等待抓帧指令
 */
class ScreenCaptureService : Service() {

    companion object {
        private const val TAG = "ScreenCapture"
        private const val EXTRA_RESULT_CODE = "result_code"
        private const val EXTRA_DATA = "data"
        private const val NOTIFICATION_ID = 2
        private const val CHANNEL_ID = "xmclaw_capture"

        /** 运行中的实例，供外部直接调用 captureAndUpload */
        @Volatile
        var instance: ScreenCaptureService? = null
            private set

        fun newIntent(context: Context, resultCode: Int, data: Intent): Intent {
            return Intent(context, ScreenCaptureService::class.java).apply {
                putExtra(EXTRA_RESULT_CODE, resultCode)
                putExtra(EXTRA_DATA, data)
            }
        }
    }

    private var mediaProjection: MediaProjection? = null
    private var virtualDisplay: VirtualDisplay? = null
    private var imageReader: ImageReader? = null

    private val serviceScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val handlerThread = HandlerThread("ScreenCapture").apply { start() }
    private val handler = Handler(handlerThread.looper)

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        instance = this
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // Android 10+ 要求 mediaProjection 服务必须处于前台
        startForeground(NOTIFICATION_ID, buildNotification())

        val resultCode = intent?.getIntExtra(EXTRA_RESULT_CODE, Activity.RESULT_CANCELED)
            ?: Activity.RESULT_CANCELED

        @Suppress("DEPRECATION")
        val data = intent?.getParcelableExtra(EXTRA_DATA) as? Intent

        if (resultCode == Activity.RESULT_OK && data != null) {
            val projectionManager = getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
            mediaProjection = projectionManager.getMediaProjection(resultCode, data)
            setupDisplay()
        }

        if (mediaProjection == null) {
            Log.e(TAG, "MediaProjection 获取失败")
            stopSelf(startId)
            return START_NOT_STICKY
        }

        return START_STICKY
    }

    @Suppress("DEPRECATION")
    private fun setupDisplay() {
        val windowManager = getSystemService(Context.WINDOW_SERVICE) as WindowManager
        val metrics = DisplayMetrics()
        windowManager.defaultDisplay.getMetrics(metrics)

        val width = metrics.widthPixels
        val height = metrics.heightPixels
        val density = metrics.densityDpi

        imageReader = ImageReader.newInstance(width, height, PixelFormat.RGBA_8888, 2)
        virtualDisplay = mediaProjection?.createVirtualDisplay(
            "XmclawScreenCapture",
            width, height, density,
            DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
            imageReader?.surface,
            null,
            handler
        )
    }

    /**
     * 抓取一帧并上传到 Daemon，返回图片 URL。
     * 需在协程中调用。
     */
    suspend fun captureAndUpload(daemonBaseUrl: String, token: String): String? {
        return withContext(Dispatchers.IO) {
            val image = imageReader?.acquireLatestImage()
            if (image == null) {
                Log.w(TAG, "acquireLatestImage 返回 null")
                return@withContext null
            }

            try {
                val planes = image.planes
                val buffer = planes[0].buffer
                val pixelStride = planes[0].pixelStride
                val rowStride = planes[0].rowStride
                val width = image.width
                val height = image.height

                // 处理 rowStride 可能大于 width * 4 的缓冲区
                val bufferWidth = rowStride / pixelStride
                val bitmap = Bitmap.createBitmap(bufferWidth, height, Bitmap.Config.ARGB_8888)
                buffer.rewind()
                bitmap.copyPixelsFromBuffer(buffer)
                val result = Bitmap.createBitmap(bitmap, 0, 0, width, height)
                bitmap.recycle()

                // 压缩为 PNG
                val stream = ByteArrayOutputStream()
                result.compress(Bitmap.CompressFormat.PNG, 100, stream)
                val pngBytes = stream.toByteArray()
                result.recycle()

                // 上传
                uploadImage(daemonBaseUrl, token, pngBytes)
            } finally {
                image.close()
            }
        }
    }

    private suspend fun uploadImage(baseUrl: String, token: String, pngBytes: ByteArray): String? {
        return withContext(Dispatchers.IO) {
            val client = OkHttpClient()
            val mediaType = "image/png".toMediaType()
            val body = MultipartBody.Builder()
                .setType(MultipartBody.FORM)
                .addFormDataPart(
                    "file", "screenshot.png",
                    pngBytes.toRequestBody(mediaType)
                )
                .build()

            val request = Request.Builder()
                .url("$baseUrl/api/v2/uploads")
                .header("Authorization", "Bearer $token")
                .post(body)
                .build()

            try {
                client.newCall(request).execute().use { response ->
                    if (!response.isSuccessful) {
                        Log.e(TAG, "上传失败: ${response.code}")
                        return@withContext null
                    }
                    val responseBody = response.body?.string() ?: return@withContext null
                    val parser = Json { ignoreUnknownKeys = true }
                    val element = parser.parseToJsonElement(responseBody)
                    element.jsonObject["url"]?.jsonPrimitive?.contentOrNull
                }
            } catch (e: Exception) {
                Log.e(TAG, "上传异常", e)
                null
            }
        }
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                "屏幕录制",
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = "XMclaw 屏幕截图服务"
            }
            val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            manager.createNotificationChannel(channel)
        }
    }

    private fun buildNotification(): Notification {
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("XMclaw 屏幕录制")
            .setContentText("正在等待截图指令")
            .setSmallIcon(android.R.drawable.ic_menu_info_details)
            .setOngoing(true)
            .build()
    }

    override fun onDestroy() {
        instance = null
        serviceScope.cancel()
        virtualDisplay?.release()
        imageReader?.close()
        mediaProjection?.stop()
        handlerThread.quitSafely()
        super.onDestroy()
    }
}
