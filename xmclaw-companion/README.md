# XMclaw Companion (Android)

> 手机端无障碍 App — XMclaw daemon 的 Android 伴侣。

## 快速开始

1. 在 Android Studio 中打开 `xmclaw-companion` 文件夹。
2. 同步 Gradle，Run 到真机或模拟器。
3. 打开 App → 开启无障碍服务 → 输入 daemon 局域网地址 + 配对码 → 连接。

## 依赖

- Android Studio Koala+
- JDK 17
- Kotlin 2.0
- Gradle 8.x
- minSdk 26 (Android 8.0) / targetSdk 34

## 架构

- `service/XmclawAccessibilityService` — 手眼核心（AccessibilityService）
- `service/ScreenCaptureService` — MediaProjection 截图
- `service/CompanionForegroundService` — 常驻前台 + 持 WS
- `link/DaemonLink` — OkHttp WebSocket 客户端 + 重连
- `link/Frame` — 协议帧编解码（kotlinx.serialization）
- `act/Actuator` — 执行 act.*（dispatchGesture / global / setText）
- `act/Blocklist` — 敏感 App 写动作拦截
- `perceive/TreeReader` — rootInActiveWindow → 扁平节点 DTO

## 协议

两端共享 `docs/android_protocol_v1.md` 中的帧契约。

## 安全

- 配对令牌存 `EncryptedSharedPreferences`
- `Blocklist` 拦截银行/支付/证券 App 的写动作
- 前台通知"XMclaw 正在控制本机" + 一键断连
