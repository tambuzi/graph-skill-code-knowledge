"""Pure-python PageRank over the symbol graph.

Used to rank symbols by structural importance for the repo map: a symbol that
is called/used/inherited by many others (transitively) scores higher. No
networkx dependency — a plain iterative power method over the edge list is
~20 lines and fast enough for repos with tens of thousands of symbols.
"""

from __future__ import annotations

from collections import defaultdict


def pagerank(
    nodes: list[str],
    edges: list[tuple[str, str]],
    damping: float = 0.85,
    iters: int = 20,
) -> dict[str, float]:
    """Rank `nodes` by incoming edge importance.

    `edges` are (src, dst) pairs; rank flows from src to dst (a caller lends
    importance to its callee). Edges touching unknown nodes are ignored.
    Returns a dict node_id -> score (scores sum to ~1).
    """
    n = len(nodes)
    if n == 0:
        return {}

    node_set = set(nodes)
    out_links: dict[str, list[str]] = defaultdict(list)
    out_degree: dict[str, int] = defaultdict(int)
    for src, dst in edges:
        if src in node_set and dst in node_set:
            out_links[src].append(dst)
            out_degree[src] += 1

    rank = {nid: 1.0 / n for nid in nodes}
    base = (1.0 - damping) / n

    for _ in range(iters):
        new = {nid: base for nid in nodes}
        dangling = 0.0
        for nid in nodes:
            if out_degree[nid] == 0:
                dangling += rank[nid]
        dangling_share = damping * dangling / n
        for src, dsts in out_links.items():
            share = damping * rank[src] / out_degree[src]
            for dst in dsts:
                new[dst] += share
        for nid in nodes:
            new[nid] += dangling_share
        rank = new

    return rank
