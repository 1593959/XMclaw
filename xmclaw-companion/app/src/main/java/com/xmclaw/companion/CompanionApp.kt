package com.xmclaw.companion

import android.app.Application
import com.xmclaw.companion.link.DaemonLink
import com.xmclaw.companion.service.XmclawAccessibilityService

/**
 * XMclaw Android Companion 全局 Application。
 * 持有 DaemonLink 与 AccessibilityService 的引用，
 * 供全应用生命周期内各组件共享。
 */
class CompanionApp : Application() {

    companion object {
        /** 全局 DaemonLink 引用，所有 Service 通过此处收发 WebSocket 消息 */
        lateinit var daemonLink: DaemonLink
            private set

        /** 当前运行的无障碍服务实例，外部可通过它调用 readTree / act */
        @Volatile
        var accessibilityService: XmclawAccessibilityService? = null
            internal set
    }

    override fun onCreate() {
        super.onCreate()
        // 初始化 DaemonLink（暂不连接，等配对后由前台服务触发）
        daemonLink = DaemonLink()
    }
}
