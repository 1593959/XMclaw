"""ACP CLI — ``xmclaw acp`` to start the IDE adapter."""
from __future__ import annotations

import typer

acp_app = typer.Typer(help="Agent Client Protocol (ACP) IDE adapter")


@acp_app.callback(invoke_without_command=True)
def acp_start() -> None:
    """Start the ACP server on stdio for IDE integration."""
    from xmclaw.acp import AcpServer
    server = AcpServer()
    server.run()
