"""XMCLaw MCP Server — exposes XMCLaw capabilities as MCP tools over stdio.

Start with: ``xmclaw mcp serve``

Architecture:
    Proma (MCP Client) ←→ stdio ←→ MCP Server (this module)
                                       ↓ imports directly
                                  XMCLaw core services
                                  (no daemon HTTP required)

The server imports XMCLaw's service layer directly — no HTTP round-trip,
no daemon dependency. Each tool lazily imports its heavy dependencies
so the server starts fast and only loads what's called.
"""
