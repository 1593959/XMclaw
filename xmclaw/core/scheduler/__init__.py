"""Evolution Scheduler — the sole decision axis of the runtime.

All "what to do next" questions flow through ``Scheduler.decide_next``.
Providers and channels never decide on their own; they emit events and let
the scheduler route. See V2_DEVELOPMENT.md §5 data-flow diagram.
"""
from xmclaw.core.scheduler.online import OnlineScheduler

__all__ = ["OnlineScheduler"]
