# XMclaw Companion — 零技术获取 APK 指南

## 最快方式：GitHub Actions 自动编译（推荐，无需安装任何软件）

### 步骤 1：下载项目 ZIP
项目已打包为 `xmclaw-companion.zip`，下载后解压到任意位置。

### 步骤 2：上传到 GitHub
1. 打开 [github.com](https://github.com)，注册/登录账号
2. 点击右上角 **+** → **New repository**
3. 仓库名填 `xmclaw-companion`，选 **Public**
4. 点击 **Create repository**
5. 在仓库页面点击 **Uploading an existing file**
6. 把解压后的所有文件拖进去，点击 **Commit changes**

### 步骤 3：等待自动编译
1. 进入仓库的 **Actions** 标签
2. 你会看到 `Build XMclaw Companion APK` 正在运行
3. 等待约 3-5 分钟（绿色 ✅ 表示成功）

### 步骤 4：下载 APK
1. 点击最新的成功运行记录
2. 下滑到 **Artifacts** 区域
3. 点击 `xmclaw-companion-debug` 下载 ZIP
4. 解压 ZIP，里面的 `.apk` 文件就是安装包

### 步骤 5：安装到手机
1. 把 APK 传到手机（微信/QQ/数据线）
2. 在手机上点击安装
3. 如果提示"未知来源"，在设置中允许安装未知应用

---

## 备用方式：找朋友帮忙

把 `xmclaw-companion.zip` 发给会用 Android Studio 的朋友，让他：
1. 用 Android Studio 打开项目
2. 点击菜单 **Build → Build Bundle(s) / APK(s) → Build APK(s)**
3. 把生成的 APK 发回给你

---

## 项目文件位置

所有文件都在你的桌面：
```
C:\Users\15978\Desktop\XMclaw\xmclaw-companion\
```

## 注意事项

- 安装前需要开启 **无障碍服务**（让 AI 控制手机）
- 需要授予 **截图权限**（让 AI 看到屏幕）
- 需要 **局域网配对**（daemon 和 手机在同一 WiFi）
