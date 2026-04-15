"""Security validation utilities."""
from pathlib import Path


def is_path_safe(target: Path, base: Path) -> bool:
    """Ensure target is within base directory."""
    try:
        target.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False
