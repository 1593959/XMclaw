# XMCLaw MCP Server wrapper — minimal startup, no typer overhead
import sys, os, logging
sys.path.insert(0, r'C:\Users\15978\Desktop\XMclaw\xmclaw')
# Silence all loggers
logging.basicConfig(level=logging.ERROR, stream=sys.stderr)
from xmclaw.mcp.server import main
main()
