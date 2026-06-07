"""File discovery + content hashing for incremental indexing."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .parser import detect_language

SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__",
    ".mypy_cache", ".pytest_cache", "dist", "build", ".next", "target",
    ".graphskill", ".idea", ".vscode", "vendor",
    # common generated / cache / artifact dirs (avoid walking & indexing them)
    "coverage", "coverage-xml", "backups", ".phpunit.cache", "storage",
    "tmp", "logs", ".php-cs-fixer.cache",
}


def iter_source_files(root: str | Path):
    """Yield (abs_path, rel_path) for files with a known language under root.

    Uses os.walk with in-place dir pruning so we never descend into excluded
    trees (vendor/, node_modules/, coverage/, dot-dirs) — critical on large
    repos where rglob would otherwise walk hundreds of thousands of files.
    """
    root = Path(root).resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune excluded + hidden dirs in place (don't recurse into them).
        dirnames[:] = [
            d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
        ]
        for fname in filenames:
            if detect_language(fname) is None:
                continue
            abs_path = Path(dirpath) / fname
            yield abs_path, abs_path.relative_to(root).as_posix()


def file_hash(path: str | Path) -> str:
    return hashlib.sha1(Path(path).read_bytes()).hexdigest()


def load_manifest(manifest_path: str | Path) -> dict[str, str]:
    p = Path(manifest_path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_manifest(manifest_path: str | Path, manifest: dict[str, str]) -> None:
    Path(manifest_path).write_text(json.dumps(manifest, indent=0, sort_keys=True))
