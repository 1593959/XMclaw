# XMclaw Companion ProGuard Rules
# Keep kotlinx.serialization
-keepattributes *Annotation*, InnerClasses, EnclosingMethod, Signature, Exceptions, *Annotation*
-keepclassmembers class kotlinx.serialization.json.** { *; }
-keepclassmembers class com.xmclaw.companion.** { *; }

# Keep OkHttp
-dontwarn okhttp3.**
-dontwarn okio.**

# Keep Compose
-keepclassmembers class androidx.compose.** { *; }

# Keep Accessibility Service
-keep public class com.xmclaw.companion.service.XmclawAccessibilityService
-keep public class com.xmclaw.companion.service.CompanionForegroundService
-keep public class com.xmclaw.companion.service.ScreenCaptureService

# Keep data classes for serialization
-keep class com.xmclaw.companion.link.Frame { *; }
-keep class com.xmclaw.companion.link.Node { *; }
-keep class com.xmclaw.companion.link.CommandResult { *; }
-keep class com.xmclaw.companion.perceive.Node { *; }
-keep class com.xmclaw.companion.ui.ChatMessage { *; }
-keep class com.xmclaw.companion.ui.ApprovalRequest { *; }
-keep class com.xmclaw.companion.ui.MissionItem { *; }
