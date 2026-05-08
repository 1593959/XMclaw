"""SkillManifest — permissions, resource limits, provenance.

Anti-req #5 + #8. Every skill ships with a manifest; runtimes refuse to
``fork`` without one. Phase 3 deliverable.

B-170: ``title`` / ``description`` are first-class so the Skills page
can show what each skill is for. SKILL.md frontmatter (skills.sh
ecosystem standard) carries those — the user_loader / proposal
materializer parse them into the manifest. ``slots=False`` (was True)
to keep ``dataclasses.asdict`` cheap and to let ``to_dict`` round-
trip without a separate codec.

B-328 — permissions are ADVISORY in the current runtime stack
(LocalSkillRuntime + ProcessSkillRuntime). They're parsed, persisted,
served by ``/api/v2/skills``, and shown in the Skills UI, but no
runtime layer enforces them — ``permissions_fs=("/tmp",)`` does NOT
prevent the skill from reading ``/etc/shadow``. This was a real gap:
operators authoring ``permissions_subprocess: []`` in SKILL.md
reasonably assumed it stopped subprocess calls, when in fact only a
docker / nsjail / firecracker runtime can enforce that and Phase 3.5+
runtime work hasn't landed.

This file does three things to surface the gap honestly without
forcing a behaviour change:

* :attr:`permissions_enforced` — bool, ``False`` until a sandbox-
  capable runtime lands. ``to_dict`` ships it so the UI can label
  permissions as "advisory" rather than "enforced".
* :func:`permissions_are_meaningful` — quick check whether the
  manifest claims any non-trivial permission constraints. Used by
  :mod:`xmclaw.skills.user_loader` for a load-time AST cross-check
  on ``permissions_subprocess`` (warns if a Python skill claims
  no-subprocess but its source uses ``subprocess.*`` / ``os.system``).
* This docstring — so future readers + audit pickers know the
  difference between *declared* and *enforced* permissions.

When a Phase 3.5+ Docker / nsjail runtime lands, it sets
``permissions_enforced=True`` on the manifests it inspects (or the
runtime's metadata says so), the UI flips the badge, and these
fields finally mean what their names imply.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class SkillManifest:
    id: str
    version: int
    title: str = ""
    description: str = ""
    permissions_fs: tuple[str, ...] = ()
    permissions_net: tuple[str, ...] = ()
    permissions_subprocess: tuple[str, ...] = ()
    max_cpu_seconds: float = 30.0
    max_memory_mb: int = 512
    created_by: str = "human"   # "human" | "user" | "llm" | "evolved"
    evidence: tuple[str, ...] = field(default_factory=tuple)
    triggers: tuple[str, ...] = field(default_factory=tuple)
    # B-328: advisory flag. Default False because LocalSkillRuntime +
    # ProcessSkillRuntime cannot enforce permissions_*; a future
    # Docker / nsjail / firecracker runtime sets this True on the
    # manifests it can actually sandbox. The Skills UI reads this to
    # show an "advisory" vs "enforced" badge so operators don't
    # mistake declarative permissions for runtime guarantees.
    permissions_enforced: bool = False

    def permissions_are_meaningful(self) -> bool:
        """B-328: True iff the manifest declares any non-trivial
        permission constraint that an operator might mistake for
        actually-enforced security.

        ``permissions_*=()`` is ambiguous (no constraint OR explicit
        deny-all) and is a more common authoring intent than the
        explicit-allowlist case, so it counts. The intent here is to
        catch the "operator wrote a permission, expects it to do
        something" path; users with truly unset manifests get nothing
        extra.

        Used by the loader to decide whether to run the AST cross-
        check + emit advisory warnings.
        """
        return bool(
            self.permissions_fs
            or self.permissions_net
            or self.permissions_subprocess
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable dict — what ``/api/v2/skills`` ships to UI.

        Tuples → lists so JSON encoders don't choke; everything else
        is already plain-data. Skill router already calls this when
        present, so adding a ``to_dict`` method beats forcing the
        router to know about dataclass internals.

        B-328: ``permissions_enforced`` rides through to the UI so
        the Skills page can render a clear "advisory" / "enforced"
        badge alongside the permission lists.
        """
        d = asdict(self)
        for k, v in list(d.items()):
            if isinstance(v, tuple):
                d[k] = list(v)
        return d
