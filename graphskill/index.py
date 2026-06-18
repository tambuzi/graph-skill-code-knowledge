"""Build orchestration: extract (cached) -> resolve -> write graph store.

Incremental strategy: tree-sitter extraction results are cached per file by
content hash, so unchanged files are never re-parsed. Name resolution and the
graph write run over the full current symbol set each build (cross-file edges
require it), which keeps the graph correct while parsing stays incremental.
A no-op build (manifest unchanged) short-circuits entirely.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil
import time
from pathlib import Path

from .indexer.embed import encode_symbols, write_sidecar
from .indexer.extract import CallSite, FileExtract, Symbol, extract_file
from .indexer.rank import pagerank
from .indexer.resolve import resolve_calls, resolve_imports, resolve_inherits, resolve_uses
from .manifest import file_hash, iter_source_files, load_manifest, save_manifest, SKIP_DIRS
from .registry import project_db_path, update_registry
from .store import GraphStore


# Bump when extraction logic changes, so stale cached extracts are invalidated.
CACHE_VERSION = 6


def default_db_path(root: str | Path) -> Path:
    """Isolated, per-project DB under ~/.graphskill/projects/<slug>/."""
    return project_db_path(root)


def _cache_key(rel_path: str, content_hash: str) -> str:
    return hashlib.sha1(f"v{CACHE_VERSION}:{rel_path}@{content_hash}".encode()).hexdigest()


def _load_cached_extract(cache_dir: Path, rel_path: str, content_hash: str) -> FileExtract | None:
    f = cache_dir / f"{_cache_key(rel_path, content_hash)}.json"
    if not f.exists():
        return None
    try:
        d = json.loads(f.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    return FileExtract(
        path=d["path"],
        lang=d["lang"],
        symbols=[Symbol(**s) for s in d["symbols"]],
        contains=[tuple(x) for x in d["contains"]],
        inherits=[tuple(x) for x in d["inherits"]],
        imports=list(d["imports"]),
        callsites=[CallSite(**c) for c in d["callsites"]],
        uses=[tuple(x) for x in d.get("uses", [])],
    )


def _save_cached_extract(cache_dir: Path, rel_path: str, content_hash: str, fx: FileExtract) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    f = cache_dir / f"{_cache_key(rel_path, content_hash)}.json"
    f.write_text(json.dumps(dataclasses.asdict(fx)))


def build_index(
    root: str | Path,
    db_path: str | Path | None = None,
    incremental: bool = True,
    log=lambda msg: None,
) -> dict:
    root = Path(root).resolve()
    db_path = Path(db_path) if db_path else default_db_path(root)
    # cache + manifest live alongside the DB, so all of a project's state is
    # isolated under one directory.
    manifest_path = db_path.parent / "manifest.json"
    cache_dir = db_path.parent / "cache"

    files = list(iter_source_files(root))
    cur_hashes = {rel: file_hash(abs_path) for abs_path, rel in files}
    old_manifest = load_manifest(manifest_path)

    if incremental and db_path.exists() and cur_hashes == old_manifest:
        log("Graph up to date — nothing changed.")
        store = GraphStore(db_path)
        stats = store.stats()
        store.close()
        update_registry(root, db=str(db_path), stats=stats, last_indexed=time.strftime('%Y-%m-%dT%H:%M:%S'))
        return stats

    changed = {rel for rel, h in cur_hashes.items() if old_manifest.get(rel) != h}
    log(f"Indexing {len(files)} files ({len(changed)} new/changed)...")

    # Extract every file (cached extracts reused for unchanged ones).
    extracts: list[FileExtract] = []
    abs_by_rel = {rel: abs_path for abs_path, rel in files}
    for rel, h in cur_hashes.items():
        fx = _load_cached_extract(cache_dir, rel, h) if incremental else None
        if fx is None:
            fx = extract_file(abs_by_rel[rel], rel)
            if fx is None:
                continue
            _save_cached_extract(cache_dir, rel, h, fx)
        extracts.append(fx)

    # Aggregate.
    all_symbols: list[Symbol] = []
    all_contains: list[tuple[str, str]] = []
    inherit_pairs: list[tuple[str, str]] = []
    callsites: list[CallSite] = []
    import_specs: list[tuple[str, str]] = []
    use_pairs: list[tuple[str, str]] = []
    for fx in extracts:
        all_symbols.extend(fx.symbols)
        all_contains.extend(fx.contains)
        inherit_pairs.extend(fx.inherits)
        callsites.extend(fx.callsites)
        import_specs.extend((fx.path, raw) for raw in fx.imports)
        use_pairs.extend(fx.uses)

    known_files = {fx.path for fx in extracts}
    import_pairs = resolve_imports(import_specs, known_files)
    call_edges = resolve_calls(all_symbols, callsites, import_pairs)
    inherit_edges = resolve_inherits(all_symbols, inherit_pairs)
    uses_edges = resolve_uses(all_symbols, use_pairs)

    # PageRank over the union of structural edges → Symbol.rank (for repo map).
    rank_edges = (
        [(a, b) for a, b, _c, _ln in call_edges]
        + [(a, b) for a, b, _c in uses_edges]
        + [(a, b) for a, b, _c in inherit_edges]
    )
    scores = pagerank([s.id for s in all_symbols], rank_edges)
    for s in all_symbols:
        s.rank = scores.get(s.id, 0.0)

    # Write a fresh graph (correctness over cleverness for cross-file edges).
    if db_path.exists():
        shutil.rmtree(db_path) if db_path.is_dir() else db_path.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = GraphStore(db_path)
    store.bulk_add_files([(fx.path, fx.lang, cur_hashes[fx.path]) for fx in extracts])
    store.bulk_add_symbols(all_symbols)
    store.bulk_add_symbol_rels("CONTAINS", all_contains, with_conf=False)
    store.bulk_add_calls(call_edges)
    store.bulk_add_symbol_rels("USES", uses_edges, with_conf=True)
    store.bulk_add_symbol_rels("INHERITS", inherit_edges, with_conf=True)
    store.bulk_add_imports(import_pairs)

    stats = store.stats()
    store.close()

    # Sidecar embeddings for semantic search (best-effort; never fail the build).
    try:
        ids, matrix = encode_symbols(all_symbols)
        write_sidecar(db_path.parent, ids, matrix)
    except Exception as exc:  # model download/encode failure shouldn't break indexing
        log(f"(embeddings skipped: {exc})")

    save_manifest(manifest_path, cur_hashes)
    update_registry(root, db=str(db_path), stats=stats, last_indexed=time.strftime('%Y-%m-%dT%H:%M:%S'))
    log("Done. " + ", ".join(f"{k}={v}" for k, v in stats.items()))
    return stats
