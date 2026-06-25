"""bash tool guardrails — Layer 1 of the sandbox plan (audit F3).

Background
==========

``builtin_shell.py`` runs ``subprocess.run(command, shell=True, ...)``
with the daemon's full user-process privileges. The audit flagged
this as a known attack surface — one successful prompt-injection +
one bash call away from ``rm -rf ~`` or worse.

The full sandbox proposal lives at ``docs/bash_sandbox_plan.md``.
This file ships Layer 1: a pattern classifier that runs BEFORE
the subprocess spawn and short-circuits on the obviously-dangerous
edges. The everyday surface (``git status``, ``pytest``, ``pip
install``) is untouched.

Classifier verdicts
-------------------

* ``allow`` — pass through. The vast majority of bash calls land here.
* ``deny`` — never run this. Return a failed ToolResult; the LLM
  sees a short explanation pointing at the matched pattern.
* ``confirm`` — risky but legitimate. The bash handler is expected
  to either (a) require a separate ``ask_user_question`` round-trip
  before proceeding, or (b) fail-closed with a clear "ask the user
  to confirm" message. In this commit we treat ``confirm`` as
  ``deny`` with a guidance message — wiring through to
  ``ask_user_question`` is a follow-up that doesn't belong in the
  guardrail module itself.

Per-environment mode
--------------------

``classify_command`` accepts ``mode``:

* ``strict`` (default) — current behavior: ``deny`` rules block,
  ``confirm`` rules require confirmation (currently fail-closed).
* ``permissive`` — ``confirm`` rules become ``allow``; ``deny``
  rules still block only genuinely catastrophic operations (fork
  bomb, disk destroyers, recursive chmod at root, etc.). This is
  the "I trust this local environment" setting.
* ``disabled`` — everything is ``allow``.

Design notes
------------

The pattern list is intentionally narrow. False positives in a
``deny`` rule are catastrophic (a normal command becomes uncallable);
false positives in ``confirm`` cost the user one extra click. So
``deny`` only matches patterns that have NO legitimate everyday
use case ("nuke the world" stuff); ``confirm`` matches anything
that's defensible but should never be a surprise (credentials
writes, autostart entries).

Interpolation: ``rm -rf "$HOME"`` and ``rm -rf ~`` are the same
attack. Patterns match both forms via wide ``[\\s'"\\$~]*``
character classes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final


@dataclass(slots=True)
class Verdict:
    decision: str  # "allow" | "deny" | "confirm"
    pattern_id: str | None = None
    reason: str = ""


# Each entry is (compiled regex, pattern_id, reason). Order doesn't
# matter — first match wins.
#
# Patterns are case-insensitive (re.IGNORECASE) and treat the
# command as a single string. They DO NOT try to parse shell
# grammar — that's a losing arms race; we match the verb + the
# obvious destructive shape and accept that a sufficiently
# motivated attacker could bypass via creative quoting. Layer 2
# (OS sandbox) is the answer to motivated attackers; Layer 1 is
# the answer to "model just tried to drop the user's home dir".

# Destructive root targets — anything in this set as the rm/chmod
# target means "user reaches the danger zone". Includes interpolated
# forms.
_DANGER_TARGETS: Final[tuple[str, ...]] = (
    "/",
    "/*",
    "~",
    "~/",
    "/usr",
    "/etc",
    "/var",
    "/bin",
    "/sbin",
    "/lib",
    "/opt",
    "/root",
    "/home",
    "$HOME",
    '"$HOME"',
    "'$HOME'",
    "${HOME}",
    "$env:USERPROFILE",
    "C:\\",
    'C:\\"',
    "C:/",
)


def _has_rm_rf_with_danger_target(text: str) -> bool:
    """True iff ``text`` contains an ``rm`` with both -r and -f flags
    pointed at a danger target.

    Pure tokenisation, no regex backtracking — easier to reason about
    + faster for short commands. Splits on whitespace; finds "rm",
    looks for a flag token starting with - that contains both r and
    f, then checks subsequent tokens against the danger list.
    """
    tokens = text.split()
    n = len(tokens)
    for i, tok in enumerate(tokens):
        if tok.lower() != "rm":
            continue
        # Scan ahead for the -rf flag (in any order) + a danger target.
        has_rf = False
        for j in range(i + 1, n):
            t = tokens[j]
            if t.startswith("-") and not t.startswith("--"):
                low = t[1:].lower()
                if "r" in low and "f" in low:
                    has_rf = True
                continue
            # First non-flag token — the target.
            if has_rf and _is_danger_target(t):
                return True
            break
    return False


def _is_danger_target(token: str) -> bool:
    """Match a token (possibly quoted / with trailing slash) against
    the danger target list."""
    s = token.strip().strip("'\"")
    if s in _DANGER_TARGETS:
        return True
    # Trailing-slash variants (``/`` vs ``/etc/``).
    if s.endswith("/") and s[:-1] in _DANGER_TARGETS:
        return True
    return False


def _has_recursive_chmod_or_chown_at_root(text: str) -> bool:
    tokens = text.split()
    n = len(tokens)
    for i, tok in enumerate(tokens):
        if tok.lower() not in ("chmod", "chown"):
            continue
        has_recursive = False
        for j in range(i + 1, n):
            t = tokens[j]
            if t.startswith("-") and not t.startswith("--"):
                if "R" in t[1:] or "r" in t[1:]:
                    has_recursive = True
                continue
            # mode (chmod) / owner (chown) comes before target —
            # skip one token.
            if j + 1 < n and has_recursive:
                target = tokens[j + 1]
                if _is_danger_target(target):
                    return True
            break
    return False


def _has_powershell_remove_root(text: str) -> bool:
    """PowerShell Remove-Item -Recurse rooted at C:\\ or $USERPROFILE."""
    low = text.lower()
    if "remove-item" not in low:
        return False
    if "-recurse" not in low and "-r " not in low:
        return False
    return any(
        d.lower() in low
        for d in ("$env:userprofile", "$home", "c:\\", "c:/")
    )


_DENY_PATTERNS: Final[tuple[tuple[re.Pattern[str], str, str], ...]] = (
    # Fork bomb.
    (
        re.compile(r":\(\)\s*\{[^}]*\|\s*:\s*&[^}]*\}\s*;\s*:"),
        "fork_bomb",
        "fork bomb pattern. Refused.",
    ),
    # dd of=/dev/...
    (
        re.compile(r"\bdd\s+[^|;&]*\bof\s*=\s*/dev/", re.IGNORECASE),
        "dd_to_device",
        "dd writing directly to a device node. Refused — this can "
        "destroy disks / partitions.",
    ),
    # mkfs.* on any block device.
    (
        re.compile(r"\bmkfs(\.\w+)?\b", re.IGNORECASE),
        "mkfs",
        "filesystem creation (mkfs). Refused — never run from an "
        "automated agent.",
    ),
    # curl | sh / wget | bash patterns.
    (
        re.compile(
            r"\b(?:curl|wget|fetch)\b[^|]*\|\s*(?:sh|bash|zsh|dash|ksh|fish)\b",
            re.IGNORECASE,
        ),
        "curl_pipe_shell",
        "piping remote download into a shell. Refused — this is "
        "the classic remote-code-execution pattern.",
    ),
    (
        re.compile(r"\bFormat-Volume\b", re.IGNORECASE),
        "powershell_format_volume",
        "Format-Volume. Refused.",
    ),
)


_TOKENISED_DENY_RULES: Final[tuple[tuple[str, str, Any], ...]] = (
    (
        "rm_rf_root_or_home",
        "rm -rf rooted at filesystem root, $HOME, ~, or a system "
        "directory. Refused — no legitimate use case from an "
        "automated agent.",
        _has_rm_rf_with_danger_target,
    ),
    (
        "chmod_chown_recursive_root",
        "recursive permission change rooted at / or a system "
        "directory. Refused.",
        _has_recursive_chmod_or_chown_at_root,
    ),
    (
        "powershell_remove_userprofile",
        "PowerShell Remove-Item -Recurse rooted at C:\\ or "
        "$USERPROFILE. Refused.",
        _has_powershell_remove_root,
    ),
)


_CONFIRM_PATTERNS: Final[tuple[tuple[re.Pattern[str], str, str], ...]] = (
    # Writes to credentials directories.
    (
        # Look for either an output redirection OR a copy/move/cat
        # command landing in one of the credentials directories.
        # Path side is permissive: ~ / $HOME / $env:USERPROFILE
        # optional prefix, then ``.<dir>/`` where <dir> is in the
        # credentials list. Quotes optional.
        re.compile(
            r"(?:[>]{1,2}|\btee\b|\bcp\b|\bmv\b|\bcat\b\s*>)"
            r"[^|;&]*?"  # anything until the target path
            r"[\"']?(?:~|\$HOME|\$env:USERPROFILE)?"
            r"[\\/]\.?"
            r"(?:ssh|aws|gnupg|config[/\\]gcloud|kube|azure)"
            r"[/\\]",
            re.IGNORECASE,
        ),
        "write_to_credentials_dir",
        "write to a credentials directory (~/.ssh, ~/.aws, "
        "~/.gnupg, ~/.kube, ~/.azure, gcloud config). Requires "
        "explicit user confirmation.",
    ),
    # Browser cookie / state files.
    (
        re.compile(
            r"[\\/](?:Cookies|cookies\.sqlite|Login Data|key3\.db|key4\.db)\b",
            re.IGNORECASE,
        ),
        "write_to_browser_state",
        "touching browser cookie / login state. Requires explicit "
        "user confirmation.",
    ),
    # Autostart / persistence mechanisms.
    (
        re.compile(r"\bcrontab\s+[-]?(e|l)?\s*<", re.IGNORECASE),
        "crontab_install",
        "crontab install. Requires explicit user confirmation — "
        "automated agents should never silently install cron jobs.",
    ),
    (
        re.compile(
            r"\bschtasks\b[^|;&]*/(?:create|change)\b",
            re.IGNORECASE,
        ),
        "schtasks_create",
        "Windows Task Scheduler create / change. Requires "
        "explicit user confirmation.",
    ),
    (
        re.compile(
            r"\blaunchctl\b[^|;&]*\b(?:load|bootstrap)\b",
            re.IGNORECASE,
        ),
        "launchctl_load",
        "macOS launchctl load. Requires explicit user confirmation.",
    ),
    (
        re.compile(
            r"\breg\s+add\b[^|;&]*\bRun(Once)?\b",
            re.IGNORECASE,
        ),
        "windows_autostart_registry",
        "Windows registry autostart entry. Requires explicit "
        "user confirmation.",
    ),
    # sudo on POSIX — defensible but never a surprise.
    (
        re.compile(r"\bsudo\b", re.IGNORECASE),
        "sudo",
        "sudo invocation. Requires explicit user confirmation — "
        "an automated agent should never silently escalate.",
    ),
)


def classify_command(
    command: str | None,
    *,
    mode: str = "strict",
) -> Verdict:
    """Return the guardrail verdict for ``command``.

    ``None`` / empty / whitespace-only commands are allowed
    through — let the upstream subprocess layer handle the "empty
    command" error path with its existing message.

    ``mode`` controls how aggressively the guardrails intervene:
    ``strict`` (default), ``permissive`` (confirm rules become allow),
    or ``disabled`` (always allow).
    """
    if not command or not command.strip():
        return Verdict(decision="allow")
    if mode == "disabled":
        return Verdict(decision="allow")
    text = command
    # Tokenised deny rules first — they're cheaper than regex and
    # cover the cases regex backtracking made fragile.
    for pid, reason, predicate in _TOKENISED_DENY_RULES:
        if predicate(text):
            return Verdict(decision="deny", pattern_id=pid, reason=reason)
    for rx, pid, reason in _DENY_PATTERNS:
        if rx.search(text):
            return Verdict(decision="deny", pattern_id=pid, reason=reason)
    for rx, pid, reason in _CONFIRM_PATTERNS:
        if rx.search(text):
            return Verdict(
                decision="allow" if mode == "permissive" else "confirm",
                pattern_id=pid,
                reason=reason,
            )
    return Verdict(decision="allow")


__all__ = ["Verdict", "classify_command"]
