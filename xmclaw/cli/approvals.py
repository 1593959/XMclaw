"""CLI for managing security approvals.

Interacts with the daemon's REST API so it works both locally and
against a remote instance.
"""
from __future__ import annotations

import json
import os
from typing import Any

import typer


_DEFAULT_BASE_URL = os.environ.get("XMC_DAEMON_URL", "http://127.0.0.1:8765")


def _api(path: str) -> str:
    return f"{_DEFAULT_BASE_URL.rstrip('/')}/api/v2{path}"


def _get(url: str) -> dict[str, Any]:
    import urllib.request

    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)


def _post(url: str) -> dict[str, Any]:
    import urllib.request

    req = urllib.request.Request(url, method="POST", data=b"", headers={})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)


def run_approvals_list() -> int:
    data = _get(_api("/approvals"))
    pending = data.get("pending", [])
    if not pending:
        typer.echo("No pending approvals.")
        return 0
    typer.echo(f"Pending approvals ({len(pending)}):")
    for item in pending:
        typer.echo(
            f"  {item['request_id']}  {item['tool_name']}  "
            f"({item['status']})  session={item['session_id']}"
        )
        # Show first line of findings summary
        summary = item.get("findings_summary", "")
        first_line = summary.splitlines()[0] if summary else ""
        if first_line:
            typer.echo(f"      {first_line}")
    return 0


def run_approvals_approve(request_id: str) -> int:
    data = _post(_api(f"/approvals/{request_id}/approve"))
    if data.get("ok"):
        typer.echo(f"Approved {request_id}.")
        return 0
    typer.echo(f"Failed: {data.get('error', 'unknown error')}", err=True)
    return 1


def run_approvals_deny(request_id: str) -> int:
    data = _post(_api(f"/approvals/{request_id}/deny"))
    if data.get("ok"):
        typer.echo(f"Denied {request_id}.")
        return 0
    typer.echo(f"Failed: {data.get('error', 'unknown error')}", err=True)
    return 1
