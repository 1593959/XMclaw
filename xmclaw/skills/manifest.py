"""SkillManifest — permissions, resource limits, provenance.

Anti-req #5 + #8. Every skill ships with a manifest; runtimes refuse to
``fork`` without one. Phase 3 deliverable.

B-170: ``title`` / ``description`` are first-class so the Skills page
can show what each skill is for. SKILL.md frontmatter (skills.sh
ecosystem standard) carries those — the user_loader / proposal
materializer parse them into the manifest. ``slots=False`` (was True)
to keep ``dataclasses.asdict`` cheap and to let ``to_dict`` round-
trip without a separate codec.

B-328 — permissions enforcement varies by runtime:

* **LocalSkillRuntime** — purely advisory. A skill can freely access
  the filesystem, network, and subprocesses regardless of manifest.
* **ProcessSkillRuntime** — Phase 3.5+ enforces:
  - filesystem sandbox (best-effort: fresh temp directory + cwd lock +
    HOME/TEMP redirect)
  - subprocess allowlist (monkey-patched ``subprocess.Popen`` guard)
  - memory soft-cap (self-monitoring daemon thread via psutil)
  - environment sanitization (sensitive env vars stripped)
  - **NOT enforced**: network isolation (``permissions_net``).
    A manifest with ``permissions_enforced=True`` and non-empty
    ``permissions_net`` is rejected by ``ProcessSkillRuntime``.
* **DockerSkillRuntime** — full kernel-enforced sandbox (fs, net,
  memory cgroup, capabilities drop).

This file does three things to surface the gap honestly:

* :attr:`permissions_enforced` — bool. ``True`` when the runtime
  that loaded the skill can actually enforce the declared limits.
  ``to_dict`` ships it so the UI can label permissions as
  "advisory" vs "enforced".
* :func:`permissions_are_meaningful` — quick check whether the
  manifest claims any non-trivial permission constraint. Used by
  :mod:`xmclaw.skills.user_loader` for a load-time AST cross-check
  on ``permissions_subprocess`` (warns if a Python skill claims
  no-subprocess but its source uses ``subprocess.*`` / ``os.system``).
* This docstring — so future readers + audit pickers know the
  difference between *declared* and *enforced* permissions.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class SkillTrustLevel(str, Enum):
    """Epic #27 P2 G-06 (2026-05-19) — trust tier for a registered
    skill, drives capability gating + force-promote eligibility.

    Tiers (low → high trust):
      * ``UNTRUSTED`` — provenance unknown OR grader marked
        ``dangerous``. Cannot be force-promoted past the gate; soft
        warnings on every invocation.
      * ``INSTALLED`` — installed via ``skill_install`` /
        ``xmclaw skill install`` from a 3rd-party source. Default
        cap: must respect ``allowed_tools`` (when declared); refused
        when grader marks dangerous.
      * ``USER`` — author is the local user (lives under
        ``~/.xmclaw/skills_user/``). Treated as code the user wrote
        themselves, so wide capability access; soft warnings when
        the source is suspicious but no hard gate.
      * ``BUILTIN`` — ships with the daemon (``xmclaw/skills/demo``
        + ``xmclaw/plugins/skills``). Fully trusted; never gated.

    Trust is set at LOAD TIME by the loader that owned the skill's
    source (UserSkillsLoader vs install_from_source vs the static
    builtin registry). Manifest authors don't get to claim a higher
    trust than their source provides — even ``trust_level=builtin``
    written into a 3rd-party SKILL.md is overridden by the loader.

    The string-valued Enum lets manifest authors write
    ``trust_level: user`` in YAML / frontmatter and round-trip
    through ``to_dict`` without bespoke serialisation.
    """
    UNTRUSTED = "untrusted"
    INSTALLED = "installed"
    USER = "user"
    BUILTIN = "builtin"


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
    # Epic #27 P1 G-10 (2026-05-19) — frontmatter expansion to match
    # the Claude Code / Hermes / Cline skill conventions so XMclaw
    # skills can round-trip with the broader ecosystem AND lay the
    # data groundwork for G-04/G-05/G-06 (which need these fields):
    #
    # - ``when_to_use``: third-person trigger guidance ("This skill
    #   should be used when..."). The Claude Code skill-development
    #   guide identifies this as the most important field for the
    #   LLM's "should I call this?" decision. Higher signal than
    #   ``description`` (which leans summary-ish).
    # - ``allowed_tools``: capability allowlist of tool names the
    #   skill may call ("file_read", "bash"). Currently advisory —
    #   G-06 will enforce via SkillTrustLevel + capability gates.
    # - ``paths``: glob patterns ("src/**/*.py") that trigger
    #   conditional activation when file_op events match. Currently
    #   stored only — G-05 wires the activation path.
    # - ``requires_restart``: True if invoking / editing this skill
    #   needs a daemon restart (Python skill.py defaults True; pure
    #   SKILL.md defaults False). UI uses to color the row.
    # - ``model``: optional model hint — when the skill explicitly
    #   requires a stronger model (``opus``) for its workflow.
    #   Empty string = inherit. Hooked by output-style / model
    #   picker future work.
    when_to_use: str = ""
    allowed_tools: tuple[str, ...] = field(default_factory=tuple)
    paths: tuple[str, ...] = field(default_factory=tuple)
    requires_restart: bool = False
    model: str = ""
    # Epic #27 P2 G-06 (2026-05-19): trust tier. Default ``user`` so
    # legacy manifests + user-authored skills don't suddenly need a
    # field they never wrote. Loaders override at load time based on
    # source — manifest author cannot self-promote past their source's
    # trust ceiling.
    trust_level: SkillTrustLevel = SkillTrustLevel.USER

    def permissions_are_meaningful(self) -> bool:
        """B-328: True iff the manifest declares any non-trivial
        permission constraint that an operator might mistake for
        actually-enforced security.

        Returns True when:
          * any of ``permissions_fs`` / ``permissions_net`` /
            ``permissions_subprocess`` is non-empty (the operator wrote
            an allowlist), OR
          * ``permissions_enforced`` is True (the operator explicitly
            opted into the runtime-sandbox contract — a strong
            engagement signal even if every list is empty, in which
            case "deny-all on empty fields" is the intended reading).

        Pre-B-341 the only signal was a non-empty list — which created
        a logic gap with :meth:`UserSkillsLoader._advisory_audit`: the
        audit's interesting case is ``permissions_subprocess == ()``
        (deny-all + source-uses-subprocess), but
        ``permissions_are_meaningful`` returned False on bare
        manifests, so the audit never ran on them. ``permissions_
        enforced=True`` gives operators a way to say "yes I mean it"
        without needing to fill an unrelated field.

        Truly unset manifests (every field at default) → False; no
        cross-check noise for skills that haven't engaged with the
        permissions system at all.
        """
        return bool(
            self.permissions_fs
            or self.permissions_net
            or self.permissions_subprocess
            or self.permissions_enforced
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
            elif isinstance(v, SkillTrustLevel):
                d[k] = v.value
        return d
