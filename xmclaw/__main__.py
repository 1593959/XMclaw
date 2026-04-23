"""Allow ``python -m xmclaw`` to invoke the CLI."""
from __future__ import annotations

from xmclaw.cli.main import app

if __name__ == "__main__":
    app()
