"""MCP server exposing the code graph as tools for Claude.

The query logic lives in ``GraphQueries`` (plain, unit-testable). ``run_server``
wraps it with FastMCP over stdio. The whole point: let Claude answer
"where/what-calls/depends-on/show-body" from the graph instead of grepping
files and reading them whole.

A ``watchdog`` observer runs in a daemon thread and re-indexes automatically
when source files change (1.5 s debounce). After each re-index the store is
swapped under a lock so in-flight queries finish before the old DB closes.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

from .manifest import SKIP_DIRS
from .parser import EXT_TO_LANG
from .store import GraphStore

_MAX_DEPTH = 6
_WATCH_EXTS = frozenset(EXT_TO_LANG.keys())


def _loc(row: dict, name_key="s.path", line_key="s.line_start") -> str:
    return f"{row.get(name_key)}:{row.get(line_key)}"


class GraphQueries:
    def __init__(self, db_path: str | Path, root: str | Path):
        self.db_path = Path(db_path)
        self.root = Path(root).resolve()
        self._lock = threading.Lock()
        self.store = GraphStore(db_path)
        self._emb_ids: list[str] | None = None
        self._emb_matrix = None
        self._load_embeddings()

    def _load_embeddings(self) -> None:
        """Load the embedding sidecar (if present) for semantic search."""
        from .indexer.embed import load_sidecar

        loaded = load_sidecar(self.db_path.parent)
        if loaded is not None:
            self._emb_ids, self._emb_matrix = loaded
        else:
            self._emb_ids, self._emb_matrix = None, None

    def _q(self, cypher: str, params: dict | None = None) -> list[dict]:
        with self._lock:
            return self.store.query(cypher, params)

    def reload_store(self) -> None:
        """Close current store and open a fresh one (called after re-index)."""
        with self._lock:
            self.store.close()
            self.store = GraphStore(self.db_path)
        self._load_embeddings()

    # ---- discovery ----
    def search_symbols(self, query: str, kind: str | None = None, limit: int = 20, offset: int = 0, compact: bool = False, visibility: str | None = None) -> list:
        cypher = (
            "MATCH (s:Symbol) WHERE lower(s.name) CONTAINS lower($q) "
            + ("AND s.kind = $kind " if kind else "")
            + ("AND s.visibility = $vis " if visibility else "")
            + "RETURN s.id AS id, s.name AS name, s.kind AS kind, "
            "s.path AS path, s.line_start AS line, s.signature AS signature, "
            "s.visibility AS visibility, s.modifiers AS modifiers "
            "ORDER BY s.name SKIP $skip LIMIT $lim"
        )
        params = {"q": query, "lim": limit, "skip": offset}
        if kind:
            params["kind"] = kind
        if visibility:
            params["vis"] = visibility
        rows = [
            {"id": r["id"], "name": r["name"], "kind": r["kind"],
             "location": f"{r['path']}:{r['line']}", "signature": r["signature"],
             "visibility": r["visibility"], "modifiers": r["modifiers"]}
            for r in self._q(cypher, params)
        ]
        if compact:
            return [f"{r['name']}\t{r['kind']}\t{r['location']}\t{r['signature']}" for r in rows]
        return rows

    def get_symbol(self, ref: str) -> dict | None:
        rows = self._q("MATCH (s:Symbol {id:$r}) RETURN s.*", {"r": ref})
        if not rows:
            rows = self._q(
                "MATCH (s:Symbol {name:$r}) RETURN s.* ORDER BY s.path LIMIT 1", {"r": ref}
            )
        if not rows:
            return None
        r = rows[0]
        return {
            "id": r["s.id"], "name": r["s.name"], "kind": r["s.kind"],
            "location": f"{r['s.path']}:{r['s.line_start']}-{r['s.line_end']}",
            "signature": r["s.signature"], "doc": r["s.doc"],
        }

    def read_symbol_body(self, ref: str) -> dict | None:
        rows = self._q("MATCH (s:Symbol {id:$r}) RETURN s.*", {"r": ref})
        if not rows:
            rows = self._q(
                "MATCH (s:Symbol {name:$r}) RETURN s.* ORDER BY s.path LIMIT 1", {"r": ref}
            )
        if not rows:
            return None
        r = rows[0]
        try:
            data = (self.root / r["s.path"]).read_bytes()[r["s.start_byte"]:r["s.end_byte"]]
            body = data.decode("utf-8", "replace")
        except OSError:
            body = ""
        return {
            "location": f"{r['s.path']}:{r['s.line_start']}-{r['s.line_end']}",
            "kind": r["s.kind"], "body": body,
        }

    # ---- graph traversal ----
    def _depth(self, depth: int) -> int:
        return max(1, min(int(depth), _MAX_DEPTH))

    def callers(self, name: str, depth: int = 1, compact: bool = False) -> list:
        d = self._depth(depth)
        if d == 1:
            rows = self._q(
                "MATCH (a:Symbol)-[e:CALLS]->(b:Symbol {name:$n}) "
                "RETURN DISTINCT a.name AS name, a.path AS path, a.line_start AS line, e.confidence AS confidence, e.line AS call_line",
                {"n": name},
            )
            results = [{"name": r["name"], "location": f"{r['path']}:{r['line']}", "confidence": r["confidence"], "call_line": r["call_line"]} for r in rows]
            if compact:
                return [f"{r['name']}\t{r['location']}\t{r['confidence']}\t@{r['call_line']}" for r in results]
            return results
        rows = self._q(
            f"MATCH (a:Symbol)-[:CALLS*1..{d}]->(b:Symbol {{name:$n}}) "
            "RETURN DISTINCT a.name AS name, a.path AS path, a.line_start AS line", {"n": name})
        results = [{"name": r["name"], "location": f"{r['path']}:{r['line']}"} for r in rows]
        if compact:
            return [f"{r['name']}\t{r['location']}" for r in results]
        return results

    def callees(self, name: str, depth: int = 1, compact: bool = False) -> list:
        d = self._depth(depth)
        if d == 1:
            rows = self._q(
                "MATCH (a:Symbol {name:$n})-[e:CALLS]->(b:Symbol) "
                "RETURN DISTINCT b.name AS name, b.path AS path, b.line_start AS line, e.confidence AS confidence, e.line AS call_line",
                {"n": name},
            )
            results = [{"name": r["name"], "location": f"{r['path']}:{r['line']}", "confidence": r["confidence"], "call_line": r["call_line"]} for r in rows]
            if compact:
                return [f"{r['name']}\t{r['location']}\t{r['confidence']}\t@{r['call_line']}" for r in results]
            return results
        rows = self._q(
            f"MATCH (a:Symbol {{name:$n}})-[:CALLS*1..{d}]->(b:Symbol) "
            "RETURN DISTINCT b.name AS name, b.path AS path, b.line_start AS line", {"n": name})
        results = [{"name": r["name"], "location": f"{r['path']}:{r['line']}"} for r in rows]
        if compact:
            return [f"{r['name']}\t{r['location']}" for r in results]
        return results

    def uses(self, name: str, compact: bool = False) -> list:
        rows = self._q(
            "MATCH (a:Symbol {name:$n})-[e:USES]->(b:Symbol) "
            "RETURN DISTINCT b.name AS name, b.kind AS kind, b.path AS path, b.line_start AS line, e.confidence AS confidence "
            "ORDER BY name", {"n": name})
        results = [{"name": r["name"], "kind": r["kind"], "location": f"{r['path']}:{r['line']}", "confidence": r["confidence"]} for r in rows]
        if compact:
            return [f"{r['name']}\t{r['kind']}\t{r['location']}\t{r['confidence']}" for r in results]
        return results

    def used_by(self, name: str, compact: bool = False) -> list:
        rows = self._q(
            "MATCH (a:Symbol)-[e:USES]->(b:Symbol {name:$n}) "
            "RETURN DISTINCT a.name AS name, a.kind AS kind, a.path AS path, a.line_start AS line, e.confidence AS confidence "
            "ORDER BY name", {"n": name})
        results = [{"name": r["name"], "kind": r["kind"], "location": f"{r['path']}:{r['line']}", "confidence": r["confidence"]} for r in rows]
        if compact:
            return [f"{r['name']}\t{r['kind']}\t{r['location']}\t{r['confidence']}" for r in results]
        return results

    def imports(self, path: str) -> list[str]:
        rows = self._q(
            "MATCH (a:File {path:$p})-[:IMPORTS]->(b:File) RETURN b.path AS path", {"p": path})
        return [r["path"] for r in rows]

    def dependents(self, path: str) -> list[str]:
        rows = self._q(
            "MATCH (a:File)-[:IMPORTS]->(b:File {path:$p}) RETURN a.path AS path", {"p": path})
        return [r["path"] for r in rows]

    def path(self, src_name: str, dst_name: str) -> list[str] | None:
        rows = self._q(
            "MATCH (a:Symbol {name:$a})-[e:CALLS* SHORTEST 1..%d]->(b:Symbol {name:$b}) "
            "RETURN list_transform(nodes(e), n -> n.name) AS mid LIMIT 1" % _MAX_DEPTH,
            {"a": src_name, "b": dst_name},
        )
        if not rows:
            return None
        return [src_name, *rows[0]["mid"], dst_name]

    def overview(self) -> dict:
        rows = self._q(
            "MATCH (s:Symbol) RETURN s.path AS path, count(s) AS n ORDER BY n DESC")
        return {"files": [{"path": r["path"], "symbols": r["n"]} for r in rows],
                "totals": self.store.stats()}

    # ---- new tools ----

    def symbols_in_file(self, path: str, compact: bool = False, visibility: str | None = None) -> list:
        cypher = (
            "MATCH (s:Symbol {path: $p}) "
            + ("WHERE s.visibility = $vis " if visibility else "")
            + "RETURN s.id AS id, s.name AS name, s.kind AS kind, "
            "s.line_start AS line_start, s.line_end AS line_end, "
            "s.signature AS signature, s.doc AS doc, "
            "s.visibility AS visibility, s.modifiers AS modifiers "
            "ORDER BY s.line_start"
        )
        params = {"p": path}
        if visibility:
            params["vis"] = visibility
        rows = self._q(cypher, params)
        results = [
            {"id": r["id"], "name": r["name"], "kind": r["kind"],
             "location": f"{path}:{r['line_start']}-{r['line_end']}",
             "signature": r["signature"], "doc": r["doc"],
             "visibility": r["visibility"], "modifiers": r["modifiers"]}
            for r in rows
        ]
        if compact:
            return [f"{r['name']}\t{r['kind']}\t{r['location']}\t{r['signature']}" for r in results]
        return results

    def batch_read_symbol_bodies(self, refs: list[str]) -> list[dict]:
        out = []
        for ref in refs:
            result = self.read_symbol_body(ref)
            if result is not None:
                result["ref"] = ref
                out.append(result)
            else:
                out.append({"ref": ref, "error": "not found"})
        return out

    def inheritors(self, name: str, depth: int = 1, compact: bool = False) -> list:
        d = self._depth(depth)
        rows = self._q(
            f"MATCH (a:Symbol)-[:INHERITS*1..{d}]->(b:Symbol {{name:$n}}) "
            "RETURN DISTINCT a.name AS name, a.kind AS kind, a.path AS path, a.line_start AS line",
            {"n": name},
        )
        results = [{"name": r["name"], "kind": r["kind"], "location": f"{r['path']}:{r['line']}"} for r in rows]
        if compact:
            return [f"{r['name']}\t{r['kind']}\t{r['location']}" for r in results]
        return results

    def inherited_from(self, name: str, depth: int = 1, compact: bool = False) -> list:
        d = self._depth(depth)
        rows = self._q(
            f"MATCH (a:Symbol {{name:$n}})-[:INHERITS*1..{d}]->(b:Symbol) "
            "RETURN DISTINCT b.name AS name, b.kind AS kind, b.path AS path, b.line_start AS line",
            {"n": name},
        )
        results = [{"name": r["name"], "kind": r["kind"], "location": f"{r['path']}:{r['line']}"} for r in rows]
        if compact:
            return [f"{r['name']}\t{r['kind']}\t{r['location']}" for r in results]
        return results

    def search_docs(self, query: str, limit: int = 20, offset: int = 0, compact: bool = False) -> list:
        rows = self._q(
            "MATCH (s:Symbol) "
            "WHERE lower(s.doc) CONTAINS lower($q) AND s.doc <> '' "
            "RETURN s.id AS id, s.name AS name, s.kind AS kind, "
            "s.path AS path, s.line_start AS line, s.doc AS doc "
            "ORDER BY s.name SKIP $skip LIMIT $lim",
            {"q": query, "lim": limit, "skip": offset},
        )
        results = [
            {"id": r["id"], "name": r["name"], "kind": r["kind"],
             "location": f"{r['path']}:{r['line']}", "doc": r["doc"]}
            for r in rows
        ]
        if compact:
            return [f"{r['name']}\t{r['kind']}\t{r['location']}" for r in results]
        return results

    def list_files(self, dir_prefix: str | None = None) -> list[str]:
        prefix = dir_prefix or ""
        rows = self._q(
            "MATCH (f:File) "
            "WHERE $prefix = '' OR f.path STARTS WITH $prefix "
            "RETURN f.path AS path ORDER BY f.path",
            {"prefix": prefix},
        )
        return [r["path"] for r in rows]

    def repo_map(self, dir_prefix: str | None = None, budget_tokens: int = 2000) -> list[str]:
        """Most structurally-important symbols (by PageRank), trimmed to a token
        budget. A whole-repo orientation view far cheaper than reading files.
        Returns compact `rank kind location signature` rows."""
        prefix = dir_prefix or ""
        rows = self._q(
            "MATCH (s:Symbol) "
            "WHERE ($prefix = '' OR s.path STARTS WITH $prefix) "
            "AND s.kind IN ['class', 'interface', 'trait', 'struct', 'enum', 'function', 'method'] "
            "RETURN s.name AS name, s.kind AS kind, s.path AS path, "
            "s.line_start AS line, s.signature AS signature, s.rank AS rank "
            "ORDER BY s.rank DESC LIMIT 500",
            {"prefix": prefix},
        )
        out: list[str] = []
        used = 0
        for r in rows:
            sig = (r["signature"] or "")[:160]
            line = f"{r['rank']:.4f}\t{r['kind']}\t{r['path']}:{r['line']}\t{sig}"
            used += len(line) // 4  # ~4 chars/token
            if used > budget_tokens:
                break
            out.append(line)
        return out

    def search_semantic(self, query: str, limit: int = 15, compact: bool = False) -> list:
        """Rank symbols by embedding cosine similarity to `query`. Finds code by
        intent even when names don't match. Falls back to [] if no embeddings."""
        if self._emb_matrix is None or not self._emb_ids:
            return []
        from .indexer.embed import encode_query, top_k

        qvec = encode_query(query)
        hits = top_k(qvec, self._emb_ids, self._emb_matrix, limit)
        results = []
        for sym_id, score in hits:
            sym = self.get_symbol(sym_id)
            if sym is None:
                continue
            sym["score"] = round(score, 4)
            results.append(sym)
        if compact:
            return [f"{r['name']}\t{r['kind']}\t{r['location']}\t{r['score']}" for r in results]
        return results

    def module_overview(self, dir_prefix: str | None = None) -> list[dict]:
        prefix = dir_prefix or ""
        rows = self._q(
            "MATCH (s:Symbol) "
            "WHERE $prefix = '' OR s.path STARTS WITH $prefix "
            "RETURN s.path AS path, s.kind AS kind",
            {"prefix": prefix},
        )
        from collections import defaultdict
        kind_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        file_sets: dict[str, set[str]] = defaultdict(set)
        for r in rows:
            parts = r["path"].split("/")
            mod = parts[0] if len(parts) > 1 else r["path"]
            kind_counts[mod][r["kind"]] += 1
            file_sets[mod].add(r["path"])
        return [
            {"module": mod, "files": len(file_sets[mod]), **dict(kind_counts[mod])}
            for mod in sorted(kind_counts)
        ]

    def hot_symbols(self, n: int = 10, edge: str = "CALLS") -> list[dict]:
        """Most-referenced symbols by incoming edge count (god nodes)."""
        valid = {"CALLS", "USES", "INHERITS"}
        if edge not in valid:
            return []
        rows = self._q(
            f"MATCH ()-[:{edge}]->(b:Symbol) "
            "RETURN b.name AS name, b.kind AS kind, b.path AS path, b.line_start AS line, count(*) AS in_degree "
            "ORDER BY in_degree DESC LIMIT $n",
            {"n": n},
        )
        return [
            {"name": r["name"], "kind": r["kind"],
             "location": f"{r['path']}:{r['line']}", "in_degree": r["in_degree"]}
            for r in rows
        ]

    def architecture_violations(self, from_prefix: str, not_to_prefix: str, edge: str = "IMPORTS") -> list[dict]:
        """Edges crossing a layer boundary: from files/symbols under from_prefix to those under not_to_prefix."""
        if edge == "IMPORTS":
            rows = self._q(
                "MATCH (a:File)-[:IMPORTS]->(b:File) "
                "WHERE a.path STARTS WITH $from AND b.path STARTS WITH $to "
                "RETURN a.path AS from_path, b.path AS to_path ORDER BY a.path",
                {"from": from_prefix, "to": not_to_prefix},
            )
            return [{"from": r["from_path"], "to": r["to_path"]} for r in rows]
        valid = {"CALLS", "USES", "INHERITS"}
        if edge not in valid:
            return []
        rows = self._q(
            f"MATCH (a:Symbol)-[:{edge}]->(b:Symbol) "
            "WHERE a.path STARTS WITH $from AND b.path STARTS WITH $to "
            "RETURN DISTINCT a.name AS from_name, a.path AS from_path, "
            "b.name AS to_name, b.path AS to_path ORDER BY a.path",
            {"from": from_prefix, "to": not_to_prefix},
        )
        return [
            {"from": f"{r['from_name']} ({r['from_path']})",
             "to": f"{r['to_name']} ({r['to_path']})"}
            for r in rows
        ]

    def subgraph(self, names: list[str], depth: int = 1) -> dict:
        d = max(1, min(int(depth), 2))
        callers_map: dict[str, dict] = {}
        callees_map: dict[str, dict] = {}
        uses_map: dict[str, dict] = {}
        used_by_map: dict[str, dict] = {}
        for name in names:
            for r in self.callers(name, d):
                callers_map[r["name"]] = r
            for r in self.callees(name, d):
                callees_map[r["name"]] = r
            for r in self.uses(name):
                uses_map[r["name"]] = r
            for r in self.used_by(name):
                used_by_map[r["name"]] = r
        focal = set(names)
        return {
            "focal": names,
            "callers": [v for k, v in callers_map.items() if k not in focal],
            "callees": [v for k, v in callees_map.items() if k not in focal],
            "uses": [v for k, v in uses_map.items() if k not in focal],
            "used_by": [v for k, v in used_by_map.items() if k not in focal],
        }

    def close(self) -> None:
        self.store.close()


class _SourceChangeHandler:
    """Debouncing watchdog event handler that triggers incremental re-index."""

    def __init__(self, root: Path, db_path: Path, gq: GraphQueries, debounce: float = 1.5):
        self._root = root
        self._db_path = db_path
        self._gq = gq
        self._debounce = debounce
        self._timer: threading.Timer | None = None
        self._timer_lock = threading.Lock()

    def _relevant(self, path: str) -> bool:
        p = Path(path)
        if p.suffix.lower() not in _WATCH_EXTS:
            return False
        try:
            parts = p.relative_to(self._root).parts
        except ValueError:
            return False
        # Skip any path whose parent components are excluded dirs or hidden dirs.
        return not any(part in SKIP_DIRS or part.startswith(".") for part in parts[:-1])

    def dispatch(self, event) -> None:
        if event.is_directory:
            return
        src = getattr(event, "src_path", "")
        dest = getattr(event, "dest_path", "")
        if not (self._relevant(src) or (dest and self._relevant(dest))):
            return
        with self._timer_lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._debounce, self._reindex)
            self._timer.daemon = True
            self._timer.start()

    def _reindex(self) -> None:
        from .index import build_index
        try:
            print("[graphskill] source changed — re-indexing...", file=sys.stderr, flush=True)
            build_index(self._root, db_path=self._db_path, incremental=True)
            self._gq.reload_store()
            print("[graphskill] re-index complete.", file=sys.stderr, flush=True)
        except Exception as exc:
            print(f"[graphskill] re-index failed: {exc}", file=sys.stderr, flush=True)


def run_server(db_path: str, root: str) -> None:
    import os
    from mcp.server.fastmcp import FastMCP
    from watchdog.observers import Observer

    db_path_obj = Path(db_path)
    root_path = Path(root).resolve()

    if not db_path_obj.exists():
        raise SystemExit(f"No graph at {db_path}. Run `graphskill index {root}` first.")

    gq = GraphQueries(db_path_obj, root_path)

    handler = _SourceChangeHandler(root_path, db_path_obj, gq)
    observer = Observer()
    observer.schedule(handler, str(root_path), recursive=True)
    observer.daemon = True
    observer.start()

    _cwd = os.getcwd()
    os.chdir(os.path.expanduser("~"))
    mcp = FastMCP("graphskill")
    os.chdir(_cwd)

    _register_tools(mcp, gq)
    mcp.run()


def estimate_tool_overhead() -> dict:
    """Per-turn input cost of the MCP tool surface (names + descriptions + param
    schemas). These bytes are sent on every turn, so they offset the per-query
    output savings. ~4 chars/token heuristic (no tiktoken dependency)."""
    import json
    import os

    from mcp.server.fastmcp import FastMCP

    _cwd = os.getcwd()
    os.chdir(os.path.expanduser("~"))
    mcp = FastMCP("graphskill")
    os.chdir(_cwd)
    _register_tools(mcp, None)  # tools aren't called, so gq may be None

    tools = mcp._tool_manager.list_tools()
    per_tool: list[tuple[str, int]] = []
    total_chars = 0
    for t in tools:
        schema = getattr(t, "parameters", None) or getattr(t, "inputSchema", {}) or {}
        chars = len(t.name) + len(getattr(t, "description", "") or "") + len(json.dumps(schema))
        total_chars += chars
        per_tool.append((t.name, chars))
    per_tool.sort(key=lambda x: -x[1])
    return {
        "tool_count": len(tools),
        "chars": total_chars,
        "tokens": total_chars // 4,
        "per_tool": per_tool,
    }


def _register_tools(mcp, gq) -> None:
    # Tool docstrings are kept to one line on purpose: they are sent as input
    # every turn, so the full usage guidance lives in SKILL.md, not here.

    @mcp.tool()
    def repo_map(dir_prefix: str | None = None, budget_tokens: int = 2000) -> list[str]:
        """Top symbols by PageRank, trimmed to a token budget. Orientation — read first."""
        return gq.repo_map(dir_prefix, budget_tokens)

    @mcp.tool()
    def search_semantic(query: str, limit: int = 15, compact: bool = False) -> list:
        """Find symbols by meaning (embedding similarity), not substring. Use for intent queries."""
        return gq.search_semantic(query, limit, compact)

    @mcp.tool()
    def search_symbols(query: str, kind: str | None = None, limit: int = 20, offset: int = 0, compact: bool = False, visibility: str | None = None) -> list:
        """Find symbols by name substring. compact→tab strings; offset paginates; visibility filters public/private."""
        return gq.search_symbols(query, kind, limit, offset, compact, visibility)

    @mcp.tool()
    def get_symbol(ref: str) -> dict | None:
        """Signature, docstring, kind, location for one symbol by id or name."""
        return gq.get_symbol(ref)

    @mcp.tool()
    def read_symbol_body(ref: str) -> dict | None:
        """Exact source of one symbol by id or name — not the whole file."""
        return gq.read_symbol_body(ref)

    @mcp.tool()
    def batch_read_symbol_bodies(refs: list[str]) -> list[dict]:
        """Read multiple symbol bodies in one call."""
        return gq.batch_read_symbol_bodies(refs)

    @mcp.tool()
    def symbols_in_file(path: str, compact: bool = False, visibility: str | None = None) -> list:
        """All symbols in a file (sig, line, visibility, modifiers). Use instead of reading the file."""
        return gq.symbols_in_file(path, compact, visibility)

    @mcp.tool()
    def callers(name: str, depth: int = 1, compact: bool = False) -> list:
        """Symbols that call `name`. depth=1 includes confidence + call_line."""
        return gq.callers(name, depth, compact)

    @mcp.tool()
    def callees(name: str, depth: int = 1, compact: bool = False) -> list:
        """Symbols called by `name`. depth=1 includes confidence + call_line."""
        return gq.callees(name, depth, compact)

    @mcp.tool()
    def uses(name: str, compact: bool = False) -> list:
        """Types `name` depends on (type-hints/new/static). Includes confidence."""
        return gq.uses(name, compact)

    @mcp.tool()
    def used_by(name: str, compact: bool = False) -> list:
        """Types that depend on `name` (reverse of uses). Includes confidence."""
        return gq.used_by(name, compact)

    @mcp.tool()
    def inheritors(name: str, depth: int = 1, compact: bool = False) -> list:
        """Classes/interfaces that inherit from or implement `name`."""
        return gq.inheritors(name, depth, compact)

    @mcp.tool()
    def inherited_from(name: str, depth: int = 1, compact: bool = False) -> list:
        """Base classes/interfaces that `name` extends or implements."""
        return gq.inherited_from(name, depth, compact)

    @mcp.tool()
    def search_docs(query: str, limit: int = 20, offset: int = 0, compact: bool = False) -> list:
        """Search docstrings/comments by substring. compact→tab strings; offset paginates."""
        return gq.search_docs(query, limit, offset, compact)

    @mcp.tool()
    def imports(path: str) -> list[str]:
        """Files imported by the given file path."""
        return gq.imports(path)

    @mcp.tool()
    def dependents(path: str) -> list[str]:
        """Files that import the given file path."""
        return gq.dependents(path)

    @mcp.tool()
    def path(src_name: str, dst_name: str) -> list[str] | None:
        """Shortest call chain between two symbols (names), or null."""
        return gq.path(src_name, dst_name)

    @mcp.tool()
    def subgraph(names: list[str], depth: int = 1) -> dict:
        """callers+callees+uses+used_by for a set of symbols in one call."""
        return gq.subgraph(names, depth)

    @mcp.tool()
    def list_files(dir_prefix: str | None = None) -> list[str]:
        """List indexed file paths, optional dir prefix filter."""
        return gq.list_files(dir_prefix)

    @mcp.tool()
    def module_overview(dir_prefix: str | None = None) -> list[dict]:
        """Symbol counts grouped by top-level directory."""
        return gq.module_overview(dir_prefix)

    @mcp.tool()
    def hot_symbols(n: int = 10, edge: str = "CALLS") -> list[dict]:
        """Most-referenced symbols by incoming edge count. edge: CALLS/USES/INHERITS."""
        return gq.hot_symbols(n, edge)

    @mcp.tool()
    def architecture_violations(from_prefix: str, not_to_prefix: str, edge: str = "IMPORTS") -> list[dict]:
        """Edges from from_prefix into not_to_prefix. edge: IMPORTS/CALLS/USES/INHERITS."""
        return gq.architecture_violations(from_prefix, not_to_prefix, edge)

    @mcp.tool()
    def overview() -> dict:
        """Per-file symbol counts plus graph totals."""
        return gq.overview()
