# XMclaw Nebula UI 替换计划

## 目标
将 `nebula-prototype.html` 中设计的完整交互 UI 替换到实际项目的 Preact 组件中。

## 技术约束
- Preact + htm ESM，无构建步骤
- 使用 `window.__xmc.preact` 和 `window.__xmc.preact_hooks`
- 保持现有数据流（props / store / API）不变，只改 UI 渲染
- CSS 使用现有 `theme-nebula.css` + 新增局部样式

## Stage 1 — Chat 页面核心（并行）

### Worker A: ChatPage + MessageList 重构
**文件**: `pages/Chat.js`, `components/molecules/MessageList.js`
**任务**:
- ChatPage 添加 HUD 条（heartbeat、记忆 facts、技能数、进化 sparkline、自主分数）
- MessageList 支持消息类型：user / assistant / system / error / warning
- 消息气泡样式：user 紫青渐变、assistant 玻璃拟态、system 居中、error 红边、warning 琥珀边
- 消息操作按钮（复制/重新生成/点赞/点踩/编辑/删除），hover 显示
- 时间戳 hover 显示
- 引用回复（quote-ref）
- 折叠长消息（is-collapsed + 展开按钮）
- 打字指示器（typing indicator）
- 流式光标（streaming cursor）

### Worker B: MessageBubble + MessageBubbleParts 重构
**文件**: `components/molecules/MessageBubble.js`, `components/molecules/MessageBubbleParts.js`
**任务**:
- Markdown 渲染：标题、列表、引用、链接、表格、分割线
- 行内代码样式
- 代码块 v2：语言标签 + 行号 + 复制/下载按钮
- 工具卡片（toolcard）：带 shimmer 动画、状态图标
- 附件网格卡片（图片/文件）
- 链接预览卡片
- 数学公式块 + 行内公式
- Artifacts 内联：Mermaid、HTML 预览、Table、SVG

### Worker C: Composer 重构
**文件**: `components/molecules/Composer.js`
**任务**:
- 玻璃拟态输入框，聚焦发光
- 自动增高 textarea
- 附件按钮 + 语音按钮 + 发送按钮
- 底部提示文字（Enter 发送 / Shift+Enter 换行 / ⌘K 命令面板）
- 拖拽上传区域（drag-drop zone）

## Stage 2 — Sessions + Settings 页面（并行）

### Worker D: SessionsPage 重构
**文件**: `pages/Sessions.js`, `pages/_panels/sessions_parts.js`
**任务**:
- 顶部统计卡片（总会话 / 今日活跃 / 消息总数 / 已归档）
- 搜索栏 + 过滤器（全部/活跃/归档）
- 会话列表项：标签（图片处理/自动化/调研等彩色 badge）
- 悬停操作按钮（归档/导出/删除）
- 删除确认对话框

### Worker E: SettingsPage 通信集成面板
**文件**: `pages/Settings.js`, 新建 `pages/_panels/settings_comm.js`
**任务**:
- 新增"通信集成"设置分类
- 8 个通信工具开关：飞书、钉钉、企业微信、Slack、Discord、邮件、Telegram、Teams
- 每个工具显示配置状态
- 消息路由规则卡片

## Stage 3 — Header + AppShell + 全局组件（并行）

### Worker F: AppHeader + AppShell 增强
**文件**: `components/organisms/AppHeader.js`, `components/organisms/AppShell.js`, `components/organisms/AppShellParts.js`
**任务**:
- Header 右侧添加通讯状态按钮（带在线指示灯）
- Header 右侧添加通知铃铛按钮
- Header 右侧添加专注模式按钮
- 通讯面板组件（WebSocket 状态、消息统计、活动会话、第三方工具）
- 通知中心面板

### Worker G: 全局交互组件
**文件**: 新建/修改 `lib/` 和 `components/` 下的全局组件
**任务**:
- 快捷键面板（⌘⇧R 重启 / ⌘⇧C 通讯 / ⌘⇧W 重连 / ? 帮助）
- 灯箱（Lightbox）组件
- 骨架屏（Skeleton）组件
- 对话框/确认框（Dialog）增强
- 离线横幅（Offline Banner）
- 拖拽上传（Dropzone）
- 评分组件（Rating）
- Diff 对比视图
- 徽章系统（Badge）扩展

## Stage 4 — 验证与汇总
主代理检查所有文件一致性，确保没有冲突，汇总变更清单。

## 文件传播（A2A）
- Stage 1 输出 → Stage 2 和 Stage 3 消费（组件引用关系）
- 各 Worker 之间无直接依赖，可完全并行
