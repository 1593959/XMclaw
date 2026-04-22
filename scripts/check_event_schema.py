"""CI-2 (V2_DEVELOPMENT.md §4.3): event-schema stability gate.

Phase 1.5 deliverable. Compares the current ``xmclaw.core.bus.events``
dataclass set against a baseline JSON schema snapshot in
``docs/schemas/events.v1.json`` and fails if breaking changes were
introduced without explicit approval.

Stub: always passes for now.
"""
from __future__ import annotations

import sys


def main() -> int:
    print("check_event_schema: stub (Phase 1.5) — always passes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
