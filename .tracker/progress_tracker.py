"""
XMclaw 开发进度追踪器
每5分钟扫描代码目录，记录已完成文件和下一步任务
"""
import os
import json
from pathlib import Path
from datetime import datetime

BASE_DIR = Path(r"C:\Users\15978\Desktop\XMclaw")
TRACKER_DIR = BASE_DIR / ".tracker"
PLAN_FILE = Path(r"C:\Users\15978\Desktop\XMclaw_DESIGN.md")

# Phase 1 MVP 完整任务清单
MVP_TASKS = [
    # 基础设施
    ("infra", "pyproject.toml", "Python 项目配置"),
    ("infra", "README.md", "项目说明"),
    
    # Daemon
    ("daemon", "xmclaw/daemon/__init__.py", "Daemon 包初始化"),
    ("daemon", "xmclaw/daemon/server.py", "FastAPI + WebSocket 服务"),
    ("daemon", "xmclaw/daemon/config.py", "配置加载"),
    ("daemon", "xmclaw/daemon/lifecycle.py", "启动/停止/守护"),
    
    # Gateway
    ("gateway", "xmclaw/gateway/__init__.py", "Gateway 包初始化"),
    ("gateway", "xmclaw/gateway/base.py", "网关抽象"),
    ("gateway", "xmclaw/gateway/websocket_gateway.py", "WebSocket 网关"),
    
    # Core
    ("core", "xmclaw/core/__init__.py", "Core 包初始化"),
    ("core", "xmclaw/core/agent_loop.py", "核心 Agent 循环"),
    ("core", "xmclaw/core/orchestrator.py", "Agent 编排器"),
    ("core", "xmclaw/core/prompt_builder.py", "Prompt 构建器"),
    ("core", "xmclaw/core/cost_tracker.py", "Token/Budget 追踪"),
    
    # LLM
    ("llm", "xmclaw/llm/__init__.py", "LLM 包初始化"),
    ("llm", "xmclaw/llm/router.py", "LLM 路由"),
    ("llm", "xmclaw/llm/openai_client.py", "OpenAI 客户端"),
    ("llm", "xmclaw/llm/anthropic_client.py", "Claude 客户端"),
    ("llm", "xmclaw/llm/streaming.py", "流式输出封装"),
    
    # Tools
    ("tools", "xmclaw/tools/__init__.py", "Tools 包初始化"),
    ("tools", "xmclaw/tools/registry.py", "工具注册表"),
    ("tools", "xmclaw/tools/base.py", "工具基类"),
    ("tools", "xmclaw/tools/environments/__init__.py", "环境包初始化"),
    ("tools", "xmclaw/tools/environments/base.py", "环境抽象基类"),
    ("tools", "xmclaw/tools/environments/local.py", "本地环境"),
    ("tools", "xmclaw/tools/file_read.py", "文件读取工具"),
    ("tools", "xmclaw/tools/file_write.py", "文件写入工具"),
    ("tools", "xmclaw/tools/file_edit.py", "文件编辑工具"),
    ("tools", "xmclaw/tools/bash.py", "命令执行工具"),
    ("tools", "xmclaw/tools/web_search.py", "网络搜索工具"),
    ("tools", "xmclaw/tools/browser.py", "浏览器工具"),
    ("tools", "xmclaw/tools/todo.py", "待办管理工具"),
    ("tools", "xmclaw/tools/memory_search.py", "记忆搜索工具"),
    
    # Memory
    ("memory", "xmclaw/memory/__init__.py", "Memory 包初始化"),
    ("memory", "xmclaw/memory/manager.py", "统一记忆管理器"),
    ("memory", "xmclaw/memory/sqlite_store.py", "SQLite 存储"),
    ("memory", "xmclaw/memory/session_manager.py", "JSONL 会话管理"),
    
    # CLI
    ("cli", "xmclaw/cli/__init__.py", "CLI 包初始化"),
    ("cli", "xmclaw/cli/main.py", "CLI 入口"),
    ("cli", "xmclaw/cli/rich_ui.py", "rich 界面"),
    ("cli", "xmclaw/cli/client.py", "Daemon 客户端"),
    
    # Utils
    ("utils", "xmclaw/utils/__init__.py", "Utils 包初始化"),
    ("utils", "xmclaw/utils/log.py", "结构化日志"),
    ("utils", "xmclaw/utils/paths.py", "路径管理"),
    
    # 配置
    ("config", "daemon/config.json", "平台级配置模板"),
    ("config", "agents/default/agent.json", "默认实例配置模板"),

    # Phase 2: 进化系统
    ("evolution", "xmclaw/evolution/__init__.py", "进化包初始化"),
    ("evolution", "xmclaw/evolution/engine.py", "进化引擎"),
    ("evolution", "xmclaw/evolution/scheduler.py", "进化调度器"),
    ("skills", "xmclaw/skills/__init__.py", "Skill 包初始化"),
    ("skills", "xmclaw/skills/manager.py", "Skill 管理器"),
    ("genes", "xmclaw/genes/__init__.py", "Gene 包初始化"),
    ("genes", "xmclaw/genes/manager.py", "Gene 管理器"),

    # Phase 3: 进化系统强化
    ("evolution", "xmclaw/evolution/vfm.py", "VFM 价值评分模型"),
    ("evolution", "xmclaw/evolution/gene_forge.py", "Gene 代码生成器"),
    ("evolution", "xmclaw/evolution/skill_forge.py", "Skill 代码生成器"),
    ("evolution", "xmclaw/evolution/validator.py", "自动验证闭环"),

    # Phase 4: Desktop UI
    ("desktop", "xmclaw/desktop/__init__.py", "Desktop 包初始化"),
    ("desktop", "xmclaw/desktop/app.py", "Desktop 应用入口"),
    ("desktop", "xmclaw/desktop/main_window.py", "主窗口"),
    ("desktop", "xmclaw/desktop/ws_client.py", "WebSocket 客户端线程"),

    # Phase 5: Web UI (fallback)
    ("web", "web/index.html", "Web 入口"),
    ("web", "web/main.js", "前端核心逻辑"),
    ("web", "web/styles.css", "样式文件"),
    ("daemon", "xmclaw/daemon/static.py", "静态文件服务"),

    # Phase 5: 打包
    ("packaging", "build.spec", "PyInstaller 配置"),
    ("packaging", "scripts/build_exe.py", "一键打包脚本"),

    # Phase 6: 多模态
    ("multimodal", "xmclaw/multimodal/__init__.py", "多模态包初始化"),
    ("multimodal", "xmclaw/multimodal/asr.py", "语音识别"),
    ("multimodal", "xmclaw/multimodal/tts.py", "语音合成"),
    ("multimodal", "xmclaw/multimodal/vision.py", "图像理解"),
]

def scan_progress():
    completed = []
    pending = []
    
    for phase, filepath, desc in MVP_TASKS:
        full_path = BASE_DIR / filepath
        if full_path.exists():
            completed.append({"phase": phase, "file": filepath, "desc": desc})
        else:
            pending.append({"phase": phase, "file": filepath, "desc": desc})
    
    return completed, pending

def record_progress():
    completed, pending = scan_progress()
    
    report = {
        "timestamp": datetime.now().isoformat(),
        "total_tasks": len(MVP_TASKS),
        "completed": len(completed),
        "pending": len(pending),
        "completion_rate": round(len(completed) / len(MVP_TASKS) * 100, 1),
        "completed_tasks": completed,
        "pending_tasks": pending[:10],  # 只显示前10个待办
        "next_steps": [t["desc"] for t in pending[:5]]
    }
    
    # 保存本次记录
    record_file = TRACKER_DIR / f"record_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(record_file, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    
    # 更新最新状态
    latest_file = TRACKER_DIR / "latest.json"
    with open(latest_file, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    
    # 追加到日志
    log_file = TRACKER_DIR / "progress.log"
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(f"[{report['timestamp']}] 完成 {report['completed']}/{report['total_tasks']} "
                f"({report['completion_rate']}%) | 接下来: {', '.join(report['next_steps'][:3])}\n")
    
    print(f"[{report['timestamp']}] 进度: {report['completed']}/{report['total_tasks']} "
          f"({report['completion_rate']}%)")
    print(f"接下来要做: {', '.join(report['next_steps'][:3])}")

if __name__ == "__main__":
    record_progress()
