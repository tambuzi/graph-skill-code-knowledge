"""Extract Symbol nodes + structural edges from a source file."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from tree_sitter import Query, QueryCursor
from tree_sitter_language_pack import get_language

from ..parser import parse_file
from . import queries as Q


@lru_cache(maxsize=512)
def _compiled_query(lang_name: str, query_src: str) -> Query:
    """Compile a query once per (language, source). Query compilation is
    expensive for large grammars (e.g. PHP ~0.3s), so caching it instead of
    recompiling per file turns thousands of compilations into a handful."""
    return Query(get_language(lang_name), query_src)


@dataclass
class Symbol:
    id: str
    name: str
    kind: str
    path: str
    lang: str
    line_start: int
    line_end: int
    start_byte: int
    end_byte: int
    signature: str
    doc: str


@dataclass
class CallSite:
    caller_id: str
    callee_name: str
    line: int


@dataclass
class FileExtract:
    path: str
    lang: str
    symbols: list[Symbol] = field(default_factory=list)
    # (parent_symbol_id, child_symbol_id)
    contains: list[tuple[str, str]] = field(default_factory=list)
    # (subclass_symbol_id, base_class_name)  -- base resolved repo-wide later
    inherits: list[tuple[str, str]] = field(default_factory=list)
    # imported module/path strings (raw, resolved to files later)
    imports: list[str] = field(default_factory=list)
    callsites: list[CallSite] = field(default_factory=list)
    # (referencing_type_symbol_id, referenced_type_name) -> resolved to USES later
    uses: list[tuple[str, str]] = field(default_factory=list)


_TYPE_KINDS = {"class", "interface", "trait", "enum", "struct"}


def _sym_id(path: str, start_byte: int) -> str:
    return f"{path}#{start_byte}"


def _first_line(text: bytes) -> str:
    return text.split(b"\n", 1)[0].decode("utf-8", "replace").strip()


def _preceding_comment(node, source: bytes) -> str:
    """Concatenate a run of comment lines immediately above a definition."""
    lines: list[str] = []
    sib = node.prev_sibling
    while sib is not None and sib.type in ("comment", "line_comment", "block_comment"):
        lines.append(sib.text.decode("utf-8", "replace").strip())
        sib = sib.prev_sibling
    return "\n".join(reversed(lines))


def _run(query_src: str, lang_name: str, root):
    """Run a (cached) query, return matches grouped per pattern as capture dicts."""
    cur = QueryCursor(_compiled_query(lang_name, query_src))
    return [caps for _idx, caps in cur.matches(root)]


def _enclosing_type(symbols: list[Symbol], byte: int) -> Symbol | None:
    """Smallest class/interface/trait/enum whose byte span contains `byte`."""
    best: Symbol | None = None
    for s in symbols:
        if s.kind in _TYPE_KINDS and s.start_byte <= byte < s.end_byte:
            if best is None or (s.end_byte - s.start_byte) < (best.end_byte - best.start_byte):
                best = s
    return best


def _enclosing(symbols: list[Symbol], byte: int, exclude_id: str | None = None) -> Symbol | None:
    """Smallest symbol whose byte span strictly contains `byte`."""
    best: Symbol | None = None
    for s in symbols:
        if s.id == exclude_id:
            continue
        if s.start_byte <= byte < s.end_byte:
            if best is None or (s.end_byte - s.start_byte) < (best.end_byte - best.start_byte):
                best = s
    return best


def extract_file(abs_path: str | Path, rel_path: str) -> FileExtract | None:
    """Parse `abs_path` and extract symbols + edges. `rel_path` keys the graph."""
    parsed = parse_file(abs_path)
    if parsed is None:
        return None
    tree, source, lang = parsed
    root = tree.root_node
    fx = FileExtract(path=rel_path, lang=lang)

    # --- definitions -> symbols ---
    def_query = Q.DEFS.get(lang)
    if def_query:
        for caps in _run(def_query, lang, root):
            name_nodes = caps.get("name")
            if not name_nodes:
                continue
            name = name_nodes[0].text.decode("utf-8", "replace")
            def_node = None
            kind = "symbol"
            for cap_name, nodes in caps.items():
                if cap_name.startswith("def.") and nodes:
                    def_node = nodes[0]
                    kind = cap_name.split(".", 1)[1]
                    break
            if def_node is None:
                continue
            fx.symbols.append(_make_symbol(def_node, name, kind, rel_path, lang, source))
    else:
        fx.symbols.extend(_generic_definitions(root, rel_path, lang, source))

    by_id = {s.id: s for s in fx.symbols}

    # --- CONTAINS (nearest enclosing symbol) ---
    for s in fx.symbols:
        parent = _enclosing(fx.symbols, s.start_byte, exclude_id=s.id)
        if parent is not None:
            fx.contains.append((parent.id, s.id))

    # --- INHERITS ---
    inh_query = Q.INHERITS.get(lang)
    if inh_query:
        for caps in _run(inh_query, lang, root):
            name_nodes = caps.get("name")
            base_nodes = caps.get("inherit")
            if not name_nodes or not base_nodes:
                continue
            cls_name = name_nodes[0].text.decode("utf-8", "replace")
            cls_sym = next(
                (s for s in fx.symbols if s.name == cls_name and s.kind in ("class", "struct", "interface")),
                None,
            )
            if cls_sym is None:
                continue
            for b in base_nodes:
                fx.inherits.append((cls_sym.id, b.text.decode("utf-8", "replace")))

    # --- IMPORTS ---
    imp_query = Q.IMPORTS.get(lang)
    if imp_query:
        for caps in _run(imp_query, lang, root):
            for n in caps.get("import", []):
                raw = n.text.decode("utf-8", "replace").strip("\"'`")
                if raw:
                    fx.imports.append(raw)

    # --- CALLS (callsites; resolved in resolve.py) ---
    call_query = Q.CALLS.get(lang)
    if call_query:
        for caps in _run(call_query, lang, root):
            for n in caps.get("call", []):
                enc = _enclosing(fx.symbols, n.start_byte)
                if enc is None:
                    continue
                fx.callsites.append(
                    CallSite(
                        caller_id=enc.id,
                        callee_name=n.text.decode("utf-8", "replace"),
                        line=n.start_point[0] + 1,
                    )
                )

    # --- USES (class -> referenced type: type-hints, new, static access) ---
    uses_query = Q.USES.get(lang)
    if uses_query:
        seen: set[tuple[str, str]] = set()
        for caps in _run(uses_query, lang, root):
            for n in caps.get("use", []):
                enc = _enclosing_type(fx.symbols, n.start_byte)
                if enc is None:
                    continue
                # last component of a possibly-qualified name (Foo\Bar -> Bar)
                name = n.text.decode("utf-8", "replace").replace("/", "\\").split("\\")[-1]
                if not name or name == enc.name:
                    continue
                key = (enc.id, name)
                if key not in seen:
                    seen.add(key)
                    fx.uses.append(key)

    return fx


def _make_symbol(node, name: str, kind: str, rel_path: str, lang: str, source: bytes) -> Symbol:
    return Symbol(
        id=_sym_id(rel_path, node.start_byte),
        name=name,
        kind=kind,
        path=rel_path,
        lang=lang,
        line_start=node.start_point[0] + 1,
        line_end=node.end_point[0] + 1,
        start_byte=node.start_byte,
        end_byte=node.end_byte,
        signature=_first_line(node.text),
        doc=_preceding_comment(node, source),
    )


def _descend_to_identifier(node, depth: int = 0):
    """Find the first identifier-like node within a shallow subtree."""
    if node.type in Q.GENERIC_NAME_TYPES:
        return node
    if depth > 3:
        return None
    for c in node.children:
        found = _descend_to_identifier(c, depth + 1)
        if found is not None:
            return found
    return None


def _generic_name(node):
    """Best-effort name for a definition node across arbitrary grammars."""
    for field in ("name", "declarator"):
        ch = node.child_by_field_name(field)
        if ch is not None:
            found = _descend_to_identifier(ch)
            if found is not None:
                return found
    # Shallow scan of children up to (but not into) the body/block.
    for c in node.children:
        if c.type in Q.GENERIC_BODY_TYPES:
            break
        found = _descend_to_identifier(c)
        if found is not None:
            return found
    return None


def _generic_definitions(root, rel_path: str, lang: str, source: bytes) -> list[Symbol]:
    """Fallback: walk the tree for definition-like nodes + a best-effort name."""
    out: list[Symbol] = []
    stack = [root]
    while stack:
        node = stack.pop()
        kind = Q.generic_kind(node.type)
        if kind:
            name_node = _generic_name(node)
            if name_node is not None:
                out.append(
                    _make_symbol(
                        node, name_node.text.decode("utf-8", "replace"), kind, rel_path, lang, source
                    )
                )
        stack.extend(node.children)
    return out
