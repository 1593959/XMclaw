"""Per-provider translators: wire format ↔ ``xmclaw.core.ir.ToolCall``.

CI-4 (tool-call IR double-direction fuzz) runs against each translator in
``tests/conformance/tool_call_ir.py``. A translator that cannot round-trip
is not allowed to ship.
"""
