package com.xmclaw.companion.link

import android.content.Context
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey

/**
 * 配对信息加密存储器。
 * 使用 EncryptedSharedPreferences 保存 device_id、token 和 daemon 地址，防止明文泄露。
 */
class Pairing(context: Context) {

    private val masterKey = MasterKey.Builder(context)
        .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
        .build()

    private val prefs = EncryptedSharedPreferences.create(
        context,
        "xmclaw_pairing",
        masterKey,
        EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
        EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM
    )

    /** 已存储的配对凭据 */
    data class Credentials(
        val token: String,
        val deviceId: String,
        val daemonHost: String
    )

    /**
     * 保存配对凭据。
     * @param token 访问令牌
     * @param deviceId 设备唯一标识
     * @param daemonHost Daemon 主机地址（如 192.168.1.2:9000）
     */
    fun save(token: String, deviceId: String, daemonHost: String) {
        prefs.edit().apply {
            putString("token", token)
            putString("device_id", deviceId)
            putString("daemon_host", daemonHost)
            apply()
        }
    }

    /**
     * 读取配对凭据。若未保存则返回 null。
     */
    fun load(): Credentials? {
        val token = prefs.getString("token", null) ?: return null
        val deviceId = prefs.getString("device_id", null) ?: return null
        val daemonHost = prefs.getString("daemon_host", null) ?: return null
        return Credentials(token, deviceId, daemonHost)
    }

    /**
     * 清除所有配对凭据。
     */
    fun clear() {
        prefs.edit().clear().apply()
    }
}
