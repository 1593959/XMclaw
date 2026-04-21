"""xmclaw.providers — all pluggable interfaces live here.

Every subpackage (llm, memory, channel, tool, runtime) exports an ``abc.ABC``
base class. Third-party plugins subclass these and register via entry-point
``xmclaw.plugins.<kind>``. See V2_DEVELOPMENT.md §3 for the contracts and
docs/REWRITE_PLAN.md §9 for the openness invariants (anti-req #13).
"""
