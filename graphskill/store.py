"""Embedded KuzuDB graph store: schema, writes, and query helpers.

All property writes use Kuzu query parameters (never string interpolation),
since signatures/docstrings contain quotes and newlines.
"""

from __future__ import annotations

from pathlib import Path

import kuzu

from .indexer.extract import Symbol

_SCHEMA = [
    "CREATE NODE TABLE IF NOT EXISTS File(path STRING, lang STRING, hash STRING, PRIMARY KEY(path))",
    """CREATE NODE TABLE IF NOT EXISTS Symbol(
        id STRING, name STRING, kind STRING, path STRING, lang STRING,
        line_start INT64, line_end INT64, start_byte INT64, end_byte INT64,
        signature STRING, doc STRING, visibility STRING, modifiers STRING,
        rank DOUBLE, PRIMARY KEY(id))""",
    "CREATE REL TABLE IF NOT EXISTS DEFINED_IN(FROM Symbol TO File)",
    "CREATE REL TABLE IF NOT EXISTS CONTAINS(FROM Symbol TO Symbol)",
    "CREATE REL TABLE IF NOT EXISTS INHERITS(FROM Symbol TO Symbol, confidence STRING)",
    "CREATE REL TABLE IF NOT EXISTS IMPORTS(FROM File TO File)",
    "CREATE REL TABLE IF NOT EXISTS CALLS(FROM Symbol TO Symbol, confidence STRING, line INT64)",
    "CREATE REL TABLE IF NOT EXISTS USES(FROM Symbol TO Symbol, confidence STRING)",
]


class GraphStore:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self.db = kuzu.Database(self.db_path)
        self.conn = kuzu.Connection(self.db)
        for stmt in _SCHEMA:
            self.conn.execute(stmt)

    # ---- transactions ----
    # Per-statement auto-commit fsyncs every write; batching thousands of
    # writes per transaction is the difference between minutes and seconds on
    # a large repo.
    def begin(self) -> None:
        self.conn.execute("BEGIN TRANSACTION")

    def commit(self) -> None:
        self.conn.execute("COMMIT")

    # ---- writes ----
    def add_file(self, path: str, lang: str, file_hash: str) -> None:
        self.conn.execute(
            "MERGE (f:File {path: $path}) SET f.lang = $lang, f.hash = $hash",
            {"path": path, "lang": lang, "hash": file_hash},
        )

    def add_symbol(self, s: Symbol) -> None:
        self.conn.execute(
            """MERGE (s:Symbol {id: $id})
               SET s.name=$name, s.kind=$kind, s.path=$path, s.lang=$lang,
                   s.line_start=$ls, s.line_end=$le, s.start_byte=$sb, s.end_byte=$eb,
                   s.signature=$sig, s.doc=$doc, s.visibility=$vis, s.modifiers=$mods,
                   s.rank=$rank""",
            {
                "id": s.id, "name": s.name, "kind": s.kind, "path": s.path, "lang": s.lang,
                "ls": s.line_start, "le": s.line_end, "sb": s.start_byte, "eb": s.end_byte,
                "sig": s.signature, "doc": s.doc, "vis": s.visibility, "mods": s.modifiers,
                "rank": s.rank,
            },
        )
        self.conn.execute(
            """MATCH (s:Symbol {id:$id}), (f:File {path:$path})
               MERGE (s)-[:DEFINED_IN]->(f)""",
            {"id": s.id, "path": s.path},
        )

    def _rel(self, a_id: str, b_id: str, rel: str, props: dict | None = None) -> None:
        set_clause = ""
        params = {"a": a_id, "b": b_id}
        if props:
            set_clause = " SET " + ", ".join(f"r.{k} = ${k}" for k in props)
            params.update(props)
        self.conn.execute(
            f"MATCH (a:Symbol {{id:$a}}), (b:Symbol {{id:$b}}) "
            f"MERGE (a)-[r:{rel}]->(b){set_clause}",
            params,
        )

    def add_contains(self, parent_id: str, child_id: str) -> None:
        self._rel(parent_id, child_id, "CONTAINS")

    def add_calls(self, a_id: str, b_id: str, confidence: str) -> None:
        self._rel(a_id, b_id, "CALLS", {"confidence": confidence})

    def add_inherits(self, a_id: str, b_id: str, confidence: str) -> None:
        self._rel(a_id, b_id, "INHERITS", {"confidence": confidence})

    def add_import(self, src_path: str, dst_path: str) -> None:
        self.conn.execute(
            """MATCH (a:File {path:$a}), (b:File {path:$b})
               MERGE (a)-[:IMPORTS]->(b)""",
            {"a": src_path, "b": dst_path},
        )

    # ---- bulk writes (fresh-build fast path) ----
    # One UNWIND execute per chunk instead of one execute per row. CREATE (not
    # MERGE) is safe because a full rebuild starts from an empty DB. This turns
    # ~80k statements into ~40, cutting initial index from minutes to seconds.
    _CHUNK = 4000

    def _chunks(self, seq):
        for i in range(0, len(seq), self._CHUNK):
            yield seq[i : i + self._CHUNK]

    def bulk_add_files(self, files: list[tuple[str, str, str]]) -> None:
        for ch in self._chunks(files):
            rows = [{"path": p, "lang": l, "hash": h} for p, l, h in ch]
            self.conn.execute(
                "UNWIND $rows AS r CREATE (:File {path: r.path, lang: r.lang, hash: r.hash})",
                {"rows": rows},
            )

    def bulk_add_symbols(self, syms: list[Symbol]) -> None:
        for ch in self._chunks(syms):
            rows = [
                {
                    "id": s.id, "name": s.name, "kind": s.kind, "path": s.path, "lang": s.lang,
                    "ls": s.line_start, "le": s.line_end, "sb": s.start_byte, "eb": s.end_byte,
                    "sig": s.signature, "doc": s.doc, "vis": s.visibility, "mods": s.modifiers,
                    "rank": s.rank,
                }
                for s in ch
            ]
            self.conn.execute(
                """UNWIND $rows AS r CREATE (:Symbol {
                    id: r.id, name: r.name, kind: r.kind, path: r.path, lang: r.lang,
                    line_start: r.ls, line_end: r.le, start_byte: r.sb, end_byte: r.eb,
                    signature: r.sig, doc: r.doc, visibility: r.vis, modifiers: r.mods,
                    rank: r.rank})""",
                {"rows": rows},
            )
        for ch in self._chunks(syms):
            rows = [{"s": s.id, "p": s.path} for s in ch]
            self.conn.execute(
                "UNWIND $rows AS r MATCH (s:Symbol {id: r.s}), (f:File {path: r.p}) "
                "CREATE (s)-[:DEFINED_IN]->(f)",
                {"rows": rows},
            )

    def bulk_add_symbol_rels(self, rel: str, edges: list[tuple], with_conf: bool) -> None:
        for ch in self._chunks(edges):
            if with_conf:
                rows = [{"a": a, "b": b, "conf": c} for a, b, c in ch]
                prop = " {confidence: r.conf}"
            else:
                rows = [{"a": a, "b": b} for a, b in ch]
                prop = ""
            self.conn.execute(
                f"UNWIND $rows AS r MATCH (a:Symbol {{id: r.a}}), (b:Symbol {{id: r.b}}) "
                f"CREATE (a)-[:{rel}{prop}]->(b)",
                {"rows": rows},
            )

    def bulk_add_calls(self, edges: list[tuple[str, str, str, int]]) -> None:
        """CALLS edges carry both confidence and the call-site line."""
        for ch in self._chunks(edges):
            rows = [{"a": a, "b": b, "conf": c, "line": ln} for a, b, c, ln in ch]
            self.conn.execute(
                "UNWIND $rows AS r MATCH (a:Symbol {id: r.a}), (b:Symbol {id: r.b}) "
                "CREATE (a)-[:CALLS {confidence: r.conf, line: r.line}]->(b)",
                {"rows": rows},
            )

    def bulk_add_imports(self, pairs: list[tuple[str, str]]) -> None:
        for ch in self._chunks(pairs):
            rows = [{"a": a, "b": b} for a, b in ch]
            self.conn.execute(
                "UNWIND $rows AS r MATCH (a:File {path: r.a}), (b:File {path: r.b}) "
                "CREATE (a)-[:IMPORTS]->(b)",
                {"rows": rows},
            )

    def delete_file(self, path: str) -> None:
        """Remove a file's symbols (+their rels) and the File node (+its rels)."""
        self.conn.execute("MATCH (s:Symbol {path:$p}) DETACH DELETE s", {"p": path})
        self.conn.execute("MATCH (f:File {path:$p}) DETACH DELETE f", {"p": path})

    # ---- reads ----
    def _rows(self, query: str, params: dict | None = None) -> list[dict]:
        res = self.conn.execute(query, params or {})
        cols = res.get_column_names()
        out = []
        while res.has_next():
            out.append(dict(zip(cols, res.get_next())))
        return out

    def stats(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for label in ("File", "Symbol"):
            out[label] = self._rows(f"MATCH (n:{label}) RETURN count(n) AS c")[0]["c"]
        for rel in ("CONTAINS", "CALLS", "USES", "INHERITS", "IMPORTS", "DEFINED_IN"):
            out[rel] = self._rows(f"MATCH ()-[r:{rel}]->() RETURN count(r) AS c")[0]["c"]
        return out

    def file_hashes(self) -> dict[str, str]:
        return {r["path"]: r["hash"] for r in self._rows("MATCH (f:File) RETURN f.path AS path, f.hash AS hash")}

    def query(self, cypher: str, params: dict | None = None) -> list[dict]:
        return self._rows(cypher, params)

    def close(self) -> None:
        self.conn.close()
        self.db.close()
