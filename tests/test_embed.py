import numpy as np
import pytest

from graphskill.indexer import embed
from graphskill.indexer.extract import Symbol


def _sym(sid, name, sig="", doc=""):
    return Symbol(
        id=sid, name=name, kind="function", path="x.py", lang="python",
        line_start=1, line_end=2, start_byte=0, end_byte=1, signature=sig, doc=doc,
    )


@pytest.fixture(scope="module")
def encoded():
    syms = [
        _sym("a", "authenticate_user", "def authenticate_user(password)", "verify login credentials"),
        _sym("b", "open_db_pool", "def open_db_pool()", "create database connection pool"),
    ]
    return embed.encode_symbols(syms)


def test_encode_shape_and_normalized(encoded):
    ids, matrix = encoded
    assert ids == ["a", "b"]
    assert matrix.shape[0] == 2
    norms = np.linalg.norm(matrix, axis=1)
    assert np.allclose(norms, 1.0, atol=1e-4)


def test_query_self_similarity(encoded):
    ids, matrix = encoded
    q = embed.encode_query("verify login credentials")
    hits = embed.top_k(q, ids, matrix, k=2)
    # the auth symbol should rank first for an auth-flavored query
    assert hits[0][0] == "a"
    assert hits[0][1] > hits[1][1]


def test_sidecar_roundtrip(tmp_path, encoded):
    ids, matrix = encoded
    embed.write_sidecar(tmp_path, ids, matrix)
    loaded = embed.load_sidecar(tmp_path)
    assert loaded is not None
    lids, lmatrix = loaded
    assert lids == ids
    assert np.allclose(lmatrix, matrix)


def test_load_sidecar_absent(tmp_path):
    assert embed.load_sidecar(tmp_path) is None


def test_top_k_empty():
    assert embed.top_k(np.zeros(4, dtype="float32"), [], None, 5) == []
