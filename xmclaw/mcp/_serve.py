# XMCLaw MCP Server wrapper — minimal startup, no typer overhead
import sys, os, logging, pathlib

# Resolve xmclaw package root dynamically so it works on any machine.
_ project_root = pathlib.Path(__file__).resolve().parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))
# Silence all loggers
logging.basicConfig(level=logging.ERROR, stream=sys.stderr)
from xmclaw.mcp.server import main
main()
