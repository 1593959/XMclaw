package com.xmclaw.companion.act

/**
 * 动作执行结果数据类。
 * 所有 Actuator 方法统一返回此结构，方便 Daemon 侧解析。
 */
data class Result(
    /** 是否执行成功 */
    val ok: Boolean,
    /** 错误信息，成功时为 null */
    val error: String? = null,
    /** 任意类型的返回数据 */
    val data: Any? = null
) {
    companion object {
        fun success(data: Any? = null): Result = Result(ok = true, data = data)
        fun fail(error: String): Result = Result(ok = false, error = error)
    }
}
