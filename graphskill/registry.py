"""Per-project storage isolation + a registry of indexed projects.

Each project gets its own directory under ``~/.graphskill/projects/<slug>/``
(graph.kuzu + cache/ + manifest.json), derived deterministically from the
project's absolute path. Two repos therefore never share or overwrite each
other's graph, and a project's server always resolves to that project's DB.

The location is outside any project tree (and outside iCloud-synced Desktop),
so sync tools never touch the database files.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path


def graphskill_home() -> Path:
    """Root for all graphskill state. Override with $GRAPHSKILL_HOME."""
    return Path(os.environ.get("GRAPHSKILL_HOME", Path.home() / ".graphskill"))


def _slug(root: str | Path) -> str:
    abs_root = str(Path(root).resolve())
    name = re.sub(r"[^A-Za-z0-9_.-]", "-", Path(abs_root).name) or "repo"
    digest = hashlib.sha1(abs_root.encode()).hexdigest()[:10]
    return f"{name}-{digest}"


def project_dir(root: str | Path) -> Path:
    """The isolated state directory for a project."""
    return graphskill_home() / "projects" / _slug(root)


def project_db_path(root: str | Path) -> Path:
    return project_dir(root) / "graph.kuzu"


# ---- registry of known projects ----

def _registry_path() -> Path:
    return graphskill_home() / "registry.json"


def load_registry() -> dict:
    p = _registry_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def update_registry(root: str | Path, **fields) -> None:
    """Record/refresh a project's entry (keyed by absolute path)."""
    abs_root = str(Path(root).resolve())
    reg = load_registry()
    entry = reg.get(abs_root, {})
    entry.update(
        {"root": abs_root, "slug": _slug(abs_root), "db": str(project_db_path(abs_root))}
    )
    entry.update(fields)
    reg[abs_root] = entry
    p = _registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(reg, indent=2, sort_keys=True))


def list_projects() -> list[dict]:
    return sorted(load_registry().values(), key=lambda e: e.get("root", ""))
