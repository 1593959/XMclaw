"""xmclaw.core — v2 runtime spine.

Packages here (bus, ir, grader, scheduler, session) form the **causal axis**
of the runtime. Architectural rule: ``core/*`` modules may not import from
``xmclaw.providers.*`` or ``xmclaw.skills.*``. Providers and skills depend
on core, never the other way. See ``tools/check_import_direction.py``.
"""
