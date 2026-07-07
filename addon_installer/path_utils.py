import os
from pathlib import Path


def is_within_dir(base: Path, target: Path) -> bool:
    """Check that target stays inside base after resolving paths."""
    try:
        return os.path.commonpath([str(base.resolve()), str(target.resolve())]) == str(base.resolve())
    except ValueError:
        return False


def safe_child_path(base: Path, name: str, label: str) -> Path:
    """Build a child path that cannot escape base."""
    target = base / name
    if not is_within_dir(base, target):
        raise RuntimeError(f"Path traversal blocked: {label}")
    return target


def safe_world_name(name: str) -> str:
    """Validate world names so they cannot become path traversal."""
    cleaned = name.strip()
    if not cleaned:
        raise RuntimeError("World name cannot be empty.")
    if Path(cleaned).is_absolute() or Path(cleaned).name != cleaned:
        raise RuntimeError(f"Unsafe world name: {name}")
    if cleaned in {".", ".."}:
        raise RuntimeError(f"Unsafe world name: {name}")
    return cleaned


def clean_name(name: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in "._- " else "_" for c in name).strip()
    return cleaned.replace(" ", "_") or "pack"
