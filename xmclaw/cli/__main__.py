"""Allow ``python -m xmclaw.cli`` to invoke the top-level Typer app."""
from __future__ import annotations

from xmclaw.cli.main import app

if __name__ == "__main__":
    app()
