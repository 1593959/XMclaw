"""Skills — versioned, manifest-declared, evolvable agent capabilities.

Anti-req #5 + #12: every skill is a version-tagged artifact. Promotion
emits a ``skill_promoted`` event with non-empty ``evidence``. Rollback is
single-call. See V2_DEVELOPMENT.md §3.5.
"""
