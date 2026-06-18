package com.xmclaw.companion.act

/**
 * 敏感应用黑名单。
 * 在执行写动作前检查前台包名，命中则拒绝执行，防止操控银行/支付类 App。
 */
object Blocklist {

    // 内置敏感包名：银行、支付、证券、保险、钱包等
    private val SENSITIVE_PACKAGES = setOf(
        // 支付宝
        "com.alipay.android.app",
        "com.eg.android.AlipayGphone",
        // 微信（支付场景）
        "com.tencent.mm",
        // 云闪付
        "com.unionpay",
        // 国有大行
        "com.chinamworld.main",         // 中国银行
        "com.android.bankabc",          // 农业银行
        "com.bankcomm.Bankcomm",        // 交通银行
        "com.cebbank.mobile.cemb",      // 光大银行
        "com.cmbchina.ccd.pluto.cmbActivity", // 招商银行
        "com.citicbank.mobilebank",     // 中信银行
        "com.hxb.mobile.client",        // 华夏银行
        "com.icbc",                     // 工商银行
        "com.pingan.paces.ccms",        // 平安银行
        "cn.com.spdb.mobilebank",       // 浦发银行
        "com.bankofchina.bocmbci",      // 中国银行（另一包名）
        "com.csii.qdg",                 // 青岛银行
        "com.cgbchina.xpt",             // 广发银行
        "com.boc.bocpay",               // 中行支付
        "com.yitong.mbank.psbc",        // 邮储银行
        "com.cmbc.cc.mbank",            // 民生银行
        // 钱包 / 支付
        "com.huawei.wallet",
        "com.samsung.android.spay",
        "com.google.android.apps.walletnfcrel",
        "com.xiaomi.wallet",
        "com.oppo.wallet",
        "com.vivo.wallet",
        // 系统敏感
        "com.android.settings",
        "com.android.packageinstaller"
    )

    // 写动作列表（clipboard 由调用方单独判断 get/set）
    private val WRITE_ACTIONS = setOf(
        "tap", "click", "input", "swipe", "key_event", "long_press"
    )

    /**
     * 判断该命令是否为写动作。
     * clipboard 需结合子命令判断，不在此列表内。
     */
    fun isWrite(cmd: String): Boolean = cmd in WRITE_ACTIONS

    /**
     * 判断包名是否命中敏感应用。
     * 支持精确匹配和前缀匹配。
     */
    fun blocked(pkg: String?): Boolean {
        if (pkg.isNullOrBlank()) return false
        return SENSITIVE_PACKAGES.any { pkg == it || pkg.startsWith("$it.") }
    }
}
