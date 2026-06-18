package com.xmclaw.companion.ui

import android.content.Context
import android.content.Intent
import android.content.SharedPreferences
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Dialog
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import com.xmclaw.companion.service.CompanionForegroundService
import com.xmclaw.companion.service.ScreenCaptureService
import kotlinx.coroutines.launch

/**
 * XMclaw Companion 主界面 —— 配对、权限引导、任务列表与聊天交互。
 * 使用 Jetpack Compose Material3 构建。
 */
class CompanionActivity : ComponentActivity() {

    /** 单条聊天消息 */
    data class ChatMessage(
        val text: String,
        val isUser: Boolean,
        val timestamp: String
    )

    /** 审批请求 */
    data class ApprovalRequest(
        val requestId: String,
        val content: String
    )

    /** Mission Control 任务项 */
    data class MissionItem(
        val time: String,
        val type: String,
        val status: String
    )

    companion object {
        private const val PREFS_NAME = "xmclaw_prefs"
        private const val KEY_TOKEN = "token"
        private const val KEY_URL = "daemon_url"

        /** 聊天消息接收回调（供 Service 调用） */
        @Volatile
        var onAgentMessage: ((String) -> Unit)? = null

        /** 审批请求接收回调（供 Service 调用） */
        @Volatile
        var onApprovalRequest: ((String, String) -> Unit)? = null

        /** 任务更新回调（供 Service 调用） */
        @Volatile
        var onMissionUpdate: ((MissionItem) -> Unit)? = null
    }

    private val mediaProjectionLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        if (result.resultCode == RESULT_OK && result.data != null) {
            val intent = ScreenCaptureService.newIntent(
                this, result.resultCode, result.data!!
            )
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                startForegroundService(intent)
            } else {
                startService(intent)
            }
        }
    }

    private val notificationPermissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { _ ->
        // 权限结果由 UI 状态自行反映，此处无需额外处理
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MaterialTheme {
                Surface(modifier = Modifier.fillMaxSize()) {
                    PairingScreen()
                }
            }
        }
    }

    @OptIn(androidx.compose.material3.ExperimentalMaterial3Api::class)
    @Composable
    fun PairingScreen() {
        val context = LocalContext.current
        val scope = rememberCoroutineScope()
        val snackbarHostState = remember { SnackbarHostState() }

        var daemonUrl by remember { mutableStateOf("") }
        var pairingCode by remember { mutableStateOf("") }
        var isPaired by remember { mutableStateOf(false) }

        // 聊天状态
        val messages = remember { mutableStateListOf<ChatMessage>() }
        var inputText by remember { mutableStateOf("") }

        // 审批弹窗状态
        var pendingApproval by remember { mutableStateOf<ApprovalRequest?>(null) }

        // Mission Control 状态
        val missionItems = remember { mutableStateListOf<MissionItem>() }

        LaunchedEffect(Unit) {
            val prefs = getEncryptedPrefs(context)
            val savedToken = prefs.getString(KEY_TOKEN, null)
            val savedUrl = prefs.getString(KEY_URL, null)
            if (!savedToken.isNullOrBlank() && !savedUrl.isNullOrBlank()) {
                daemonUrl = savedUrl
                isPaired = true
                CompanionForegroundService.start(context, savedUrl, savedToken)
            }

            // 注册 Service → UI 回调
            onAgentMessage = { text ->
                messages.add(ChatMessage(text = text, isUser = false, timestamp = ""))
            }
            onApprovalRequest = { requestId, content ->
                pendingApproval = ApprovalRequest(requestId, content)
            }
            onMissionUpdate = { item ->
                missionItems.add(item)
                // 最多保留 50 条
                if (missionItems.size > 50) {
                    missionItems.removeAt(0)
                }
            }
        }

        Scaffold(
            snackbarHost = { SnackbarHost(snackbarHostState) },
            topBar = { TopAppBar(title = { Text("XMclaw Companion") }) }
        ) { padding ->
            Column(
                modifier = Modifier
                    .fillMaxSize()
                    .padding(padding)
                    .padding(16.dp),
                verticalArrangement = Arrangement.spacedBy(12.dp)
            ) {
                if (isPaired) {
                    // 已配对：任务/聊天视图
                    Text(
                        text = "已配对: $daemonUrl",
                        style = MaterialTheme.typography.bodyLarge
                    )
                    Button(
                        onClick = {
                            CompanionForegroundService.stop(context)
                            clearCredentials(context)
                            isPaired = false
                            daemonUrl = ""
                            pairingCode = ""
                            messages.clear()
                            missionItems.clear()
                            scope.launch {
                                snackbarHostState.showSnackbar("已断开并清除凭证")
                            }
                        },
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Text("断开连接")
                    }

                    // Mission Control 移动投影
                    Text(
                        text = "Mission Control",
                        style = MaterialTheme.typography.titleMedium
                    )
                    Card(
                        modifier = Modifier
                            .fillMaxWidth()
                            .heightIn(max = 120.dp)
                    ) {
                        LazyColumn(
                            modifier = Modifier
                                .fillMaxWidth()
                                .padding(8.dp)
                        ) {
                            items(missionItems.reversed()) { item ->
                                Text(
                                    text = "${item.time} | ${item.type} | ${item.status}",
                                    style = MaterialTheme.typography.bodySmall
                                )
                            }
                        }
                    }

                    Divider()

                    // 聊天消息列表
                    LazyColumn(
                        modifier = Modifier
                            .fillMaxWidth()
                            .weight(1f)
                    ) {
                        items(messages) { msg ->
                            val alignment = if (msg.isUser) Alignment.CenterEnd else Alignment.CenterStart
                            val bgColor = if (msg.isUser) {
                                MaterialTheme.colorScheme.primaryContainer
                            } else {
                                MaterialTheme.colorScheme.secondaryContainer
                            }
                            Box(
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .padding(vertical = 4.dp),
                                contentAlignment = alignment
                            ) {
                                Surface(
                                    color = bgColor,
                                    shape = MaterialTheme.shapes.medium,
                                    modifier = Modifier.padding(horizontal = 8.dp, vertical = 4.dp)
                                ) {
                                    Text(
                                        text = msg.text,
                                        modifier = Modifier.padding(8.dp),
                                        style = MaterialTheme.typography.bodyMedium
                                    )
                                }
                            }
                        }
                    }

                    // 底部输入栏
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(8.dp)
                    ) {
                        OutlinedTextField(
                            value = inputText,
                            onValueChange = { inputText = it },
                            label = { Text("发送消息给 Agent") },
                            modifier = Modifier.weight(1f),
                            singleLine = true
                        )
                        Button(
                            onClick = {
                                if (inputText.isNotBlank()) {
                                    messages.add(ChatMessage(
                                        text = inputText,
                                        isUser = true,
                                        timestamp = ""
                                    ))
                                    CompanionForegroundService.sendUserMessage(inputText)
                                    inputText = ""
                                }
                            }
                        ) {
                            Text("发送")
                        }
                    }
                } else {
                    // 未配对：配对界面
                    OutlinedTextField(
                        value = daemonUrl,
                        onValueChange = { daemonUrl = it },
                        label = { Text("Daemon 地址") },
                        placeholder = { Text("ws://192.168.1.5:8080") },
                        modifier = Modifier.fillMaxWidth(),
                        singleLine = true
                    )
                    OutlinedTextField(
                        value = pairingCode,
                        onValueChange = { pairingCode = it },
                        label = { Text("配对码") },
                        modifier = Modifier.fillMaxWidth(),
                        singleLine = true
                    )
                    Button(
                        onClick = {
                            if (daemonUrl.isBlank() || pairingCode.isBlank()) {
                                scope.launch {
                                    snackbarHostState.showSnackbar("请填写完整信息")
                                }
                                return@Button
                            }
                            // Android 13+ 需要通知权限才能启动前台服务
                            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                                notificationPermissionLauncher.launch(
                                    android.Manifest.permission.POST_NOTIFICATIONS
                                )
                            }
                            saveCredentials(context, daemonUrl, pairingCode)
                            isPaired = true
                            CompanionForegroundService.start(context, daemonUrl, pairingCode)
                            scope.launch {
                                snackbarHostState.showSnackbar("配对成功，服务已启动")
                            }
                        },
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Text("配对并启动")
                    }
                }

                Divider()

                Text("权限检查", style = MaterialTheme.typography.titleMedium)

                PermissionItem(
                    name = "无障碍服务",
                    granted = isAccessibilityEnabled(context),
                    onClick = { openAccessibilitySettings(context) }
                )
                PermissionItem(
                    name = "通知权限",
                    granted = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                        checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) ==
                                android.content.pm.PackageManager.PERMISSION_GRANTED
                    } else true,
                    onClick = {
                        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                            notificationPermissionLauncher.launch(
                                android.Manifest.permission.POST_NOTIFICATIONS
                            )
                        }
                    }
                )
                PermissionItem(
                    name = "屏幕录制",
                    granted = false,
                    onClick = { requestScreenCapture() }
                )
            }
        }

        // 审批弹窗
        if (pendingApproval != null) {
            Dialog(onDismissRequest = { /* 不可通过点击外部关闭 */ }) {
                Card(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(16.dp),
                    shape = MaterialTheme.shapes.large
                ) {
                    Column(
                        modifier = Modifier.padding(16.dp),
                        verticalArrangement = Arrangement.spacedBy(12.dp)
                    ) {
                        Text(
                            text = "审批请求",
                            style = MaterialTheme.typography.titleLarge
                        )
                        Text(
                            text = pendingApproval!!.content,
                            style = MaterialTheme.typography.bodyMedium
                        )
                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            horizontalArrangement = Arrangement.spacedBy(8.dp, Alignment.End)
                        ) {
                            OutlinedButton(
                                onClick = {
                                    CompanionForegroundService.sendApproval(
                                        pendingApproval!!.requestId,
                                        "allow_always"
                                    )
                                    pendingApproval = null
                                }
                            ) {
                                Text("总是允许")
                            }
                            Button(
                                onClick = {
                                    CompanionForegroundService.sendApproval(
                                        pendingApproval!!.requestId,
                                        "allow"
                                    )
                                    pendingApproval = null
                                }
                            ) {
                                Text("允许")
                            }
                            Button(
                                onClick = {
                                    CompanionForegroundService.sendApproval(
                                        pendingApproval!!.requestId,
                                        "deny"
                                    )
                                    pendingApproval = null
                                },
                                colors = ButtonDefaults.buttonColors(
                                    containerColor = MaterialTheme.colorScheme.error
                                )
                            ) {
                                Text("拒绝")
                            }
                        }
                    }
                }
            }
        }
    }

    @Composable
    private fun PermissionItem(name: String, granted: Boolean, onClick: () -> Unit) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(vertical = 4.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.SpaceBetween
        ) {
            Text(text = name, style = MaterialTheme.typography.bodyMedium)
            Button(onClick = onClick) {
                Text(if (granted) "已开启" else "去开启")
            }
        }
    }

    private fun requestScreenCapture() {
        val manager = getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
        val intent = manager.createScreenCaptureIntent()
        mediaProjectionLauncher.launch(intent)
    }

    private fun isAccessibilityEnabled(context: Context): Boolean {
        val enabledServices = Settings.Secure.getString(
            context.contentResolver,
            Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES
        ) ?: return false
        return enabledServices.contains(context.packageName)
    }

    private fun openAccessibilitySettings(context: Context) {
        val intent = Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS).apply {
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        context.startActivity(intent)
    }

    private fun getEncryptedPrefs(context: Context): SharedPreferences {
        val masterKey = MasterKey.Builder(context)
            .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
            .build()
        return EncryptedSharedPreferences.create(
            context,
            PREFS_NAME,
            masterKey,
            EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
            EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM
        )
    }

    private fun saveCredentials(context: Context, url: String, token: String) {
        val prefs = getEncryptedPrefs(context)
        prefs.edit().apply {
            putString(KEY_URL, url)
            putString(KEY_TOKEN, token)
            apply()
        }
    }

    private fun clearCredentials(context: Context) {
        val prefs = getEncryptedPrefs(context)
        prefs.edit().apply {
            remove(KEY_URL)
            remove(KEY_TOKEN)
            apply()
        }
    }
}
