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

    def _q(self, cypher: str, params: dict | None = None) -> list[dict]:
        with self._lock:
            return self.store.query(cypher, params)

    def reload_store(self) -> None:
        """Close current store and open a fresh one (called after re-index)."""
        with self._lock:
            self.store.close()
            self.store = GraphStore(self.db_path)

    # ---- discovery ----
    def search_symbols(self, query: str, kind: str | None = None, limit: int = 20) -> list[dict]:
        cypher = (
            "MATCH (s:Symbol) WHERE lower(s.name) CONTAINS lower($q) "
            + ("AND s.kind = $kind " if kind else "")
            + "RETURN s.id AS id, s.name AS name, s.kind AS kind, "
            "s.path AS path, s.line_start AS line, s.signature AS signature "
            "ORDER BY s.name LIMIT $lim"
        )
        params = {"q": query, "lim": limit}
        if kind:
            params["kind"] = kind
        return [
            {"id": r["id"], "name": r["name"], "kind": r["kind"],
             "location": f"{r['path']}:{r['line']}", "signature": r["signature"]}
            for r in self._q(cypher, params)
        ]

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

    def callers(self, name: str, depth: int = 1) -> list[dict]:
        d = self._depth(depth)
        rows = self._q(
            f"MATCH (a:Symbol)-[:CALLS*1..{d}]->(b:Symbol {{name:$n}}) "
            "RETURN DISTINCT a.name AS name, a.path AS path, a.line_start AS line", {"n": name})
        return [{"name": r["name"], "location": f"{r['path']}:{r['line']}"} for r in rows]

    def callees(self, name: str, depth: int = 1) -> list[dict]:
        d = self._depth(depth)
        rows = self._q(
            f"MATCH (a:Symbol {{name:$n}})-[:CALLS*1..{d}]->(b:Symbol) "
            "RETURN DISTINCT b.name AS name, b.path AS path, b.line_start AS line", {"n": name})
        return [{"name": r["name"], "location": f"{r['path']}:{r['line']}"} for r in rows]

    def uses(self, name: str) -> list[dict]:
        rows = self._q(
            "MATCH (a:Symbol {name:$n})-[:USES]->(b:Symbol) "
            "RETURN DISTINCT b.name AS name, b.kind AS kind, b.path AS path, b.line_start AS line "
            "ORDER BY name", {"n": name})
        return [{"name": r["name"], "kind": r["kind"], "location": f"{r['path']}:{r['line']}"} for r in rows]

    def used_by(self, name: str) -> list[dict]:
        rows = self._q(
            "MATCH (a:Symbol)-[:USES]->(b:Symbol {name:$n}) "
            "RETURN DISTINCT a.name AS name, a.kind AS kind, a.path AS path, a.line_start AS line "
            "ORDER BY name", {"n": name})
        return [{"name": r["name"], "kind": r["kind"], "location": f"{r['path']}:{r['line']}"} for r in rows]

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

    @mcp.tool()
    def search_symbols(query: str, kind: str | None = None, limit: int = 20) -> list[dict]:
        """Find symbols by name substring. Returns id, kind, location, signature. Use this before grep."""
        return gq.search_symbols(query, kind, limit)

    @mcp.tool()
    def get_symbol(ref: str) -> dict | None:
        """Get a symbol's signature, docstring, kind and location by id or name."""
        return gq.get_symbol(ref)

    @mcp.tool()
    def read_symbol_body(ref: str) -> dict | None:
        """Return the exact source of one symbol (function/class) by id or name — not the whole file."""
        return gq.read_symbol_body(ref)

    @mcp.tool()
    def callers(name: str, depth: int = 1) -> list[dict]:
        """Symbols that call `name` (transitively up to depth)."""
        return gq.callers(name, depth)

    @mcp.tool()
    def callees(name: str, depth: int = 1) -> list[dict]:
        """Symbols called by `name` (transitively up to depth)."""
        return gq.callees(name, depth)

    @mcp.tool()
    def uses(name: str) -> list[dict]:
        """Classes/interfaces/traits that `name` (a class) depends on — via type-hints, `new`, or static access."""
        return gq.uses(name)

    @mcp.tool()
    def used_by(name: str) -> list[dict]:
        """Classes that depend on `name` (reverse of `uses`)."""
        return gq.used_by(name)

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
        """Shortest call chain from one symbol to another (list of names), or null."""
        return gq.path(src_name, dst_name)

    @mcp.tool()
    def overview() -> dict:
        """Per-file symbol counts plus graph totals — for orientation."""
        return gq.overview()

    mcp.run()
