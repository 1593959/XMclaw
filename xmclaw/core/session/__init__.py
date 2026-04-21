"""Session lifecycle — create → active → checkpoint → destroy.

Anti-requirement #9: sessions may not leak state across runs. Lifecycle is
explicit and observable (emits ``session_lifecycle`` events).
"""
from xmclaw.core.session.lifecycle import Session, SessionPhase

__all__ = ["Session", "SessionPhase"]
