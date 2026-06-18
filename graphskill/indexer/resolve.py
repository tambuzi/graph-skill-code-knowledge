"""Heuristic name resolution: turn extracted names into edges between symbols.

Pure tree-sitter has no cross-file name resolution, so call/inherit/import
targets are resolved by name with a confidence tag:

* ``EXTRACTED``  — unique match (repo-wide, or unique within the caller's file).
* ``INFERRED``   — disambiguated via an import edge from the caller's file.
* ``AMBIGUOUS``  — multiple plausible targets; all linked (capped).

Unresolved names (stdlib/3rd-party) are dropped — they are not nodes.
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from pathlib import Path

from .extract import CallSite, Symbol

_TYPE_KINDS = {"class", "struct", "interface", "type", "trait", "enum"}
_AMBIGUOUS_CAP = 5


def resolve_imports(
    import_specs: list[tuple[str, str]], known_files: set[str]
) -> list[tuple[str, str]]:
    """Map (src_path, raw_module) -> (src_path, dst_path) for in-repo targets."""
    by_stem: dict[str, list[str]] = defaultdict(list)
    no_ext: dict[str, str] = {}
    for p in known_files:
        by_stem[Path(p).stem].append(p)
        no_ext[Path(p).with_suffix("").as_posix()] = p

    pairs: set[tuple[str, str]] = set()
    for src, raw in import_specs:
        target: str | None = None
        if raw.startswith("."):
            base = os.path.normpath((Path(src).parent / raw).as_posix())
            if base in no_ext:
                target = no_ext[base]
            else:
                cands = by_stem.get(Path(raw).name, [])
                target = cands[0] if len(cands) == 1 else None
        else:
            last = re.split(r"[./\\]", raw)[-1]
            cands = by_stem.get(last, [])
            if len(cands) == 1:
                target = cands[0]
            elif raw in no_ext:
                target = no_ext[raw]
        if target and target != src:
            pairs.add((src, target))
    return list(pairs)


def resolve_calls(
    symbols: list[Symbol],
    callsites: list[CallSite],
    import_pairs: list[tuple[str, str]],
) -> list[tuple[str, str, str, int]]:
    """Resolve call sites -> (caller_id, callee_id, confidence, line).

    `line` is the source line of the (first) call site for that caller→callee
    pair, so callers()/callees() can point at the exact call without a body read.
    """
    by_name: dict[str, list[Symbol]] = defaultdict(list)
    for s in symbols:
        by_name[s.name].append(s)
    path_of = {s.id: s.path for s in symbols}
    imports_by_file: dict[str, set[str]] = defaultdict(set)
    for src, dst in import_pairs:
        imports_by_file[src].add(dst)

    # keyed (caller, callee) -> (confidence, line); keep the first line seen.
    edges: dict[tuple[str, str], tuple[str, int]] = {}

    def _add(caller: str, callee: str, conf: str, line: int) -> None:
        if (caller, callee) not in edges:
            edges[(caller, callee)] = (conf, line)

    for cs in callsites:
        cands = by_name.get(cs.callee_name, [])
        if not cands:
            continue
        caller_path = path_of.get(cs.caller_id)
        if len(cands) == 1:
            _add(cs.caller_id, cands[0].id, "EXTRACTED", cs.line)
            continue
        same_file = [c for c in cands if c.path == caller_path]
        if len(same_file) == 1:
            _add(cs.caller_id, same_file[0].id, "EXTRACTED", cs.line)
            continue
        imported = [c for c in cands if c.path in imports_by_file.get(caller_path, set())]
        if len(imported) == 1:
            _add(cs.caller_id, imported[0].id, "INFERRED", cs.line)
            continue
        for c in cands[:_AMBIGUOUS_CAP]:
            _add(cs.caller_id, c.id, "AMBIGUOUS", cs.line)
    return [(a, b, conf, line) for (a, b), (conf, line) in edges.items()]


def resolve_uses(
    symbols: list[Symbol], use_pairs: list[tuple[str, str]]
) -> list[tuple[str, str, str]]:
    """Resolve class->referenced-type names to USES edges (same name-matching as
    inheritance), dropping self-references."""
    return [(a, b, c) for a, b, c in resolve_inherits(symbols, use_pairs) if a != b]


def resolve_inherits(
    symbols: list[Symbol], inherit_pairs: list[tuple[str, str]]
) -> list[tuple[str, str, str]]:
    """Resolve (subclass_id, base_name) -> (subclass_id, base_id, confidence)."""
    by_name: dict[str, list[Symbol]] = defaultdict(list)
    for s in symbols:
        if s.kind in _TYPE_KINDS:
            by_name[s.name].append(s)
    path_of = {s.id: s.path for s in symbols}

    edges: set[tuple[str, str, str]] = set()
    for sub_id, base_name in inherit_pairs:
        cands = by_name.get(base_name, [])
        if not cands:
            continue
        if len(cands) == 1:
            edges.add((sub_id, cands[0].id, "EXTRACTED"))
            continue
        same_file = [c for c in cands if c.path == path_of.get(sub_id)]
        if len(same_file) == 1:
            edges.add((sub_id, same_file[0].id, "EXTRACTED"))
        else:
            for c in cands[:_AMBIGUOUS_CAP]:
                edges.add((sub_id, c.id, "AMBIGUOUS"))
    return list(edges)
