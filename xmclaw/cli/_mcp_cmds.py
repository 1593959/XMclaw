"""``xmclaw mcp`` — MCP server subcommands."""
from __future__ import annotations

import typer

mcp_app = typer.Typer(help="MCP (Model Context Protocol) server for Proma ↔ XMCLaw bridge")


@mcp_app.command(name="serve")
def mcp_serve() -> None:
    """Start the MCP stdio server for Proma integration.

    Proma connects via stdio MCP — no HTTP, no auth, local-only.
    Run this from Proma's mcp.json config:

        {
          "servers": {
            "xmclaw": {
              "command": "xmclaw",
              "args": ["mcp", "serve"]
            }
          }
        }
    """
    import sys
    import logging
    # Silence noisy loggers so they don't pollute the MCP stdio channel
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    from xmclaw.mcp.server import main
    main()
