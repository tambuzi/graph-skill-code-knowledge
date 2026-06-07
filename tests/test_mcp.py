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
