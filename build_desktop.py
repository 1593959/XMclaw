"""Build script for XMclaw desktop app using PyInstaller."""
import PyInstaller.__main__
import os

BASE = os.path.dirname(os.path.abspath(__file__))

# Collect hidden imports for dynamic modules
hidden_imports = [
    "xmclaw.daemon.server",
    "xmclaw.daemon.config",
    "xmclaw.daemon.lifecycle",
    "xmclaw.daemon.static",
    "xmclaw.core.orchestrator",
    "xmclaw.core.agent_loop",
    "xmclaw.core.prompt_builder",
    "xmclaw.core.cost_tracker",
    "xmclaw.core.reflection",
    "xmclaw.tools.base",
    "xmclaw.tools.registry",
    "xmclaw.tools.file_read",
    "xmclaw.tools.file_write",
    "xmclaw.tools.file_edit",
    "xmclaw.tools.bash",
    "xmclaw.tools.browser",
    "xmclaw.tools.web_search",
    "xmclaw.tools.web_fetch",
    "xmclaw.tools.todo",
    "xmclaw.tools.task_tool",
    "xmclaw.tools.ask_user",
    "xmclaw.tools.agent_tool",
    "xmclaw.tools.skill_tool",
    "xmclaw.tools.memory_search",
    "xmclaw.tools.glob_tool",
    "xmclaw.tools.grep_tool",
    "xmclaw.tools.git",
    "xmclaw.tools.computer_use",
    "xmclaw.tools.test_tool",
    "xmclaw.tools.mcp_tool",
    "xmclaw.memory.manager",
    "xmclaw.memory.session_manager",
    "xmclaw.memory.sqlite_store",
    "xmclaw.memory.vector_store",
    "xmclaw.evolution.scheduler",
    "xmclaw.evolution.engine",
    "xmclaw.evolution.gene_forge",
    "xmclaw.evolution.skill_forge",
    "xmclaw.evolution.validator",
    "xmclaw.evolution.vfm",
    "xmclaw.cli.main",
]

args = [
    os.path.join(BASE, "xmclaw", "desktop", "app.py"),
    "--name", "XMclaw",
    "--windowed",
    "--noconfirm",
    "--clean",
    "--distpath", os.path.join(BASE, "dist"),
    "--workpath", os.path.join(BASE, "build"),
    "--specpath", BASE,
]

for mod in hidden_imports:
    args.extend(["--hidden-import", mod])

# Include web UI static files and agent templates
args.extend([
    "--add-data", f"{os.path.join(BASE, 'web')}{os.pathsep}web",
    "--add-data", f"{os.path.join(BASE, 'agents')}{os.pathsep}agents",
    "--add-data", f"{os.path.join(BASE, 'shared')}{os.pathsep}shared",
    "--add-data", f"{os.path.join(BASE, 'daemon')}{os.pathsep}daemon",
])

PyInstaller.__main__.run(args)
