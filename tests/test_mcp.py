import pytest

from graphskill.index import build_index, default_db_path
from graphskill.mcp_server import GraphQueries


@pytest.fixture
def gq(sample_repo):
    build_index(sample_repo)
    q = GraphQueries(default_db_path(sample_repo), sample_repo)
    yield q
    q.close()


def test_search_symbols(gq):
    res = gq.search_symbols("login")
    names = {r["name"] for r in res}
    assert names == {"login"}
    assert all("location" in r and "signature" in r for r in res)


def test_read_symbol_body_is_single_symbol(gq):
    body = gq.read_symbol_body("auth.py#235")["body"]
    assert body.startswith("def login(self, password):")
    assert "return token" in body
    assert "class User" not in body  # only the function, not the file


def test_callers_and_callees(gq):
    assert {c["name"] for c in gq.callers("connect")} == {"login"}
    deep = {c["name"] for c in gq.callees("login", depth=2)}
    assert {"connect", "hash_password", "open_pool"} <= deep


def test_path_and_imports(gq):
    assert gq.path("login", "open_pool") == ["login", "connect", "open_pool"]
    assert gq.path("login", "nope") is None
    assert gq.imports("auth.py") == ["db.py"]
    assert gq.dependents("auth.py") == ["service.ts"]


def test_overview(gq):
    ov = gq.overview()
    assert ov["totals"]["Symbol"] == 12
    assert any(f["path"] == "auth.py" for f in ov["files"])


def test_symbols_in_file(gq):
    syms = gq.symbols_in_file("auth.py")
    names = {s["name"] for s in syms}
    assert {"hash_password", "BaseUser", "User", "name", "login"} == names
    assert all("location" in s and "signature" in s for s in syms)
    # ordered by line
    starts = [int(s["location"].split(":")[1].split("-")[0]) for s in syms]
    assert starts == sorted(starts)


def test_symbols_in_file_empty(gq):
    assert gq.symbols_in_file("nonexistent.py") == []


def test_batch_read_symbol_bodies(gq):
    results = gq.batch_read_symbol_bodies(["login", "connect"])
    found = {r["ref"] for r in results if "body" in r}
    assert found == {"login", "connect"}
    assert all("error" not in r for r in results if r["ref"] in found)


def test_batch_read_symbol_bodies_missing(gq):
    results = gq.batch_read_symbol_bodies(["nonexistent_xyz"])
    assert results[0]["error"] == "not found"


def test_inheritors(gq):
    inh = gq.inheritors("BaseUser")
    assert {r["name"] for r in inh} == {"User"}
    assert gq.inheritors("User") == []


def test_inherited_from(gq):
    base = gq.inherited_from("User")
    assert {r["name"] for r in base} == {"BaseUser"}
    assert gq.inherited_from("BaseUser") == []


def test_search_docs(gq):
    res = gq.search_docs("connection pool")
    assert any(r["name"] == "connect" for r in res)
    assert all("doc" in r and "location" in r for r in res)


def test_search_docs_no_match(gq):
    assert gq.search_docs("xyzzy_no_match_42") == []


def test_list_files(gq):
    files = gq.list_files()
    assert set(files) == {"auth.py", "db.py", "handler.go", "service.ts"}


def test_list_files_prefix(gq):
    assert gq.list_files("auth") == ["auth.py"]
    assert gq.list_files("no_match_prefix") == []


def test_subgraph(gq):
    sg = gq.subgraph(["login"])
    assert sg["focal"] == ["login"]
    callee_names = {r["name"] for r in sg["callees"]}
    assert {"connect", "hash_password"} <= callee_names


def test_confidence_callers_depth1(gq):
    callers_list = gq.callers("connect")
    assert len(callers_list) > 0
    assert all("confidence" in r for r in callers_list)


def test_confidence_callers_depth2(gq):
    deep = gq.callers("open_pool", depth=2)
    assert len(deep) > 0
    assert all("confidence" not in r for r in deep)


def test_confidence_callees_depth1(gq):
    callees_list = gq.callees("login")
    assert len(callees_list) > 0
    assert all("confidence" in r for r in callees_list)
