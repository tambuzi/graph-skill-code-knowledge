from graphskill.indexer.rank import pagerank


def test_pagerank_sink_ranks_highest():
    # A -> B -> C, A -> C : C is a pure sink, should rank above the source A.
    nodes = ["A", "B", "C"]
    edges = [("A", "B"), ("B", "C"), ("A", "C")]
    scores = pagerank(nodes, edges)
    assert scores["C"] > scores["A"]
    assert abs(sum(scores.values()) - 1.0) < 1e-6


def test_pagerank_empty():
    assert pagerank([], []) == {}


def test_pagerank_ignores_unknown_nodes():
    scores = pagerank(["A", "B"], [("A", "B"), ("A", "ghost"), ("ghost", "B")])
    assert set(scores) == {"A", "B"}
    assert scores["B"] > scores["A"]
