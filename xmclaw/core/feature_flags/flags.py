"""Feature-flag data types.

A *flag* is a named knob with:

* ``name`` — stable id (e.g. ``"cognition.idle_aware_scheduling"``)
* ``default`` — value when nothing overrides
* ``description`` — operator-visible note

A *variant* is a typed value (bool / int / float / str / dict).
``is_enabled`` is sugar for ``variant_of(...) == True`` on bool
flags.

Why types beyond bool: real feature flags want experiments —
"window=1h vs 24h", "top_k=12 vs 25" — not just on/off. Variants
keep that ergonomic.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Union

# A flag's value is one of these JSON-shape primitives. Lists/dicts
# kept lightweight; nesting allowed but the resolver treats them as
# opaque blobs (not partial overrides).
Variant = Union[bool, int, float, str, list, dict, None]


@dataclass(frozen=True, slots=True)
class FeatureFlag:
    """Schema for a single flag.

    Register once at module load (typically in
    ``xmclaw.core.feature_flags.registry``); the resolver looks the
    schema up by name when a call site asks for the variant.
    """

    name: str
    default: Variant = False
    description: str = ""

    def env_var(self) -> str:
        """The env-var override name for this flag.

        Convention: ``XMC_FF_<NAME>`` with dots → underscores +
        uppercased. So ``cognition.idle_aware`` →
        ``XMC_FF_COGNITION_IDLE_AWARE``.
        """
        return "XMC_FF_" + self.name.upper().replace(".", "_").replace("-", "_")


__all__ = ["FeatureFlag", "Variant"]
