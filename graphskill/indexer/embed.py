"""Local symbol embeddings via model2vec (static, no torch).

Embeddings power `search_semantic`: map a natural-language intent ("validate a
token") to symbols whose name/signature/doc are semantically close, instead of
substring matching. The model is a small static embedder (~30MB) that runs
fully on CPU; it is fetched once from the HuggingFace hub and cached locally,
after which encoding is offline.

Vectors are stored as a sidecar `embeddings.npy` (L2-normalized float32) plus
`embedding_ids.json` (row → symbol id) next to the graph DB, so the Kuzu
database stays portable and a query is a single numpy matmul.
"""

from __future__ import annotations

import json
from pathlib import Path

_MODEL_NAME = "minishlab/potion-base-8M"
_model = None  # lazily loaded singleton

EMB_FILE = "embeddings.npy"
IDS_FILE = "embedding_ids.json"


def _get_model():
    global _model
    if _model is None:
        from model2vec import StaticModel

        _model = StaticModel.from_pretrained(_MODEL_NAME)
    return _model


def _symbol_text(name: str, signature: str, doc: str) -> str:
    return " ".join(p for p in (name, signature, doc) if p).strip()


def _normalize(matrix):
    import numpy as np

    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (matrix / norms).astype("float32")


def encode_symbols(symbols) -> tuple[list[str], "object"]:
    """Encode symbols → (ids, normalized float32 matrix). Empty-safe."""
    import numpy as np

    if not symbols:
        return [], np.zeros((0, 0), dtype="float32")
    ids = [s.id for s in symbols]
    texts = [_symbol_text(s.name, s.signature, s.doc) for s in symbols]
    matrix = _get_model().encode(texts)
    return ids, _normalize(np.asarray(matrix, dtype="float32"))


def encode_query(text: str):
    """Encode a single query string → normalized float32 vector."""
    import numpy as np

    vec = np.asarray(_get_model().encode([text]), dtype="float32")
    return _normalize(vec)[0]


def write_sidecar(out_dir: Path, ids: list[str], matrix) -> None:
    import numpy as np

    out_dir.mkdir(parents=True, exist_ok=True)
    np.save(out_dir / EMB_FILE, matrix)
    (out_dir / IDS_FILE).write_text(json.dumps(ids))


def load_sidecar(out_dir: Path) -> tuple[list[str], "object"] | None:
    """Load (ids, matrix) sidecar, or None if absent/unreadable."""
    import numpy as np

    emb = out_dir / EMB_FILE
    idf = out_dir / IDS_FILE
    if not (emb.exists() and idf.exists()):
        return None
    try:
        matrix = np.load(emb)
        ids = json.loads(idf.read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return ids, matrix


def top_k(query_vec, ids: list[str], matrix, k: int) -> list[tuple[str, float]]:
    """Cosine top-k (vectors are pre-normalized → dot product). Returns
    (symbol_id, score) descending."""
    import numpy as np

    if matrix is None or len(ids) == 0 or matrix.size == 0:
        return []
    scores = matrix @ query_vec
    k = min(k, len(ids))
    idx = np.argpartition(-scores, k - 1)[:k]
    idx = idx[np.argsort(-scores[idx])]
    return [(ids[i], float(scores[i])) for i in idx]
