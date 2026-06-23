from graphskill.index import build_index, default_db_path
from graphskill.store import GraphStore


def test_build_index_stats(sample_repo):
    stats = build_index(sample_repo)
    assert stats["File"] == 4
    assert stats["Symbol"] == 12
    assert stats["CALLS"] == 5
    assert stats["INHERITS"] == 1
    assert stats["IMPORTS"] == 2


def test_calls_resolved_with_confidence(sample_repo):
    build_index(sample_repo)
    store = GraphStore(default_db_path(sample_repo))
    rows = store.query(
        "MATCH (a:Symbol {name:'login', path:'auth.py'})-[c:CALLS]->(b:Symbol) "
        "RETURN b.name AS name, c.confidence AS conf ORDER BY b.name"
    )
    store.close()
    by = {r["name"]: r["conf"] for r in rows}
    assert by["connect"] == "EXTRACTED"
    assert by["hash_password"] == "EXTRACTED"


def test_imports_resolved_to_files(sample_repo):
    build_index(sample_repo)
    store = GraphStore(default_db_path(sample_repo))
    rows = store.query("MATCH (a:File)-[:IMPORTS]->(b:File) RETURN a.path AS a, b.path AS b")
    store.close()
    pairs = {(r["a"], r["b"]) for r in rows}
    assert ("auth.py", "db.py") in pairs
    assert ("service.ts", "auth.py") in pairs


def test_python_relative_imports_resolve(tmp_path):
    repo = tmp_path / "pkg"
    repo.mkdir()
    (repo / "store.py").write_text("def save():\n    return 1\n")
    (repo / "core.py").write_text("from .store import save\n\ndef run():\n    return save()\n")
    build_index(repo)
    store = GraphStore(default_db_path(repo))
    pairs = {
        (r["a"], r["b"])
        for r in store.query("MATCH (a:File)-[:IMPORTS]->(b:File) RETURN a.path AS a, b.path AS b")
    }
    store.close()
    assert ("core.py", "store.py") in pairs


def test_calls_have_line(sample_repo):
    build_index(sample_repo)
    store = GraphStore(default_db_path(sample_repo))
    rows = store.query(
        "MATCH (a:Symbol {name:'login'})-[c:CALLS]->(b:Symbol {name:'connect'}) "
        "RETURN c.line AS line"
    )
    store.close()
    assert rows and rows[0]["line"] == 18  # `conn = connect()` in auth.py


def test_symbols_have_rank_and_visibility(sample_repo):
    build_index(sample_repo)
    store = GraphStore(default_db_path(sample_repo))
    rows = store.query(
        "MATCH (s:Symbol) RETURN s.name AS name, s.rank AS rank, s.visibility AS vis"
    )
    store.close()
    by = {r["name"]: r for r in rows}
    assert all(r["rank"] is not None for r in rows)
    assert sum(r["rank"] for r in rows) > 0
    # open_pool is a called sink; Handle is an uncalled caller — sink outranks.
    # (both names are unique in the fixture, avoiding cross-file collisions)
    assert by["open_pool"]["rank"] > by["Handle"]["rank"]
    assert by["connect"]["vis"] == "public"


def test_schema_bump_forces_rebuild(sample_repo, monkeypatch):
    build_index(sample_repo)
    db = default_db_path(sample_repo)
    sv = db.parent / "schema_version"
    assert sv.exists()

    # Simulate an older-schema DB: stale version marker, source files unchanged.
    sv.write_text("0")
    logs: list[str] = []
    build_index(sample_repo, log=logs.append)
    # Must NOT short-circuit — it must rebuild and refresh the version marker.
    assert not any("up to date" in m for m in logs)
    assert any("Schema changed" in m for m in logs)
    from graphskill.store import SCHEMA_VERSION
    assert sv.read_text().strip() == str(SCHEMA_VERSION)


def test_incremental_noop_and_change(sample_repo):
    build_index(sample_repo)
    db = default_db_path(sample_repo)

    # no change -> short-circuit (no re-index work)
    logs: list[str] = []
    build_index(sample_repo, log=logs.append)
    assert any("up to date" in m for m in logs)

    # change a file -> new symbol appears
    (sample_repo / "db.py").write_text(
        "def connect():\n    return open_pool()\n\n"
        "def open_pool():\n    return {}\n\n"
        "def reset():\n    return True\n"
    )
    stats = build_index(sample_repo)
    assert stats["Symbol"] == 13
    store = GraphStore(db)
    found = store.query("MATCH (s:Symbol {name:'reset'}) RETURN s.path AS p")
    store.close()
    assert found and found[0]["p"] == "db.py"
