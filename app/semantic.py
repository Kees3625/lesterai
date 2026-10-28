"""Semantic search over document chunks using local Ollama embeddings.

Embeddings are computed by the Ollama embedding model and stored as JSON vectors
in the `vectors` table. Retrieval combines semantic similarity (cosine) with the
keyword/BM25 search via reciprocal-rank fusion, so exact terms (e.g. "SPI", "VPN")
and paraphrases both work. Everything degrades gracefully to keyword-only search if
the embedding service is unavailable.
"""

import json
import math

import httpx

from . import config, db


def embed_text(text: str) -> list[float] | None:
    try:
        r = httpx.post(
            f"{config.OLLAMA_URL}/api/embeddings",
            json={"model": config.OLLAMA_EMBED_MODEL, "prompt": text},
            timeout=30.0,
        )
        if r.status_code == 200:
            vec = r.json().get("embedding")
            return vec if vec else None
    except httpx.HTTPError:
        return None
    return None


def index_document(doc_id: int, chunks: list[str]) -> None:
    """Embed each chunk and store the vectors for this document."""
    items = []
    for c in chunks:
        vec = embed_text(c)
        if vec:
            items.append((c, json.dumps(vec)))
    if items:
        db.replace_vectors(doc_id, items)
    else:
        db.delete_vectors(doc_id)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def semantic_search(query: str, limit: int = 8) -> list[dict]:
    qv = embed_text(query)
    if not qv:
        return []
    scored = []
    for row in db.all_vectors():
        try:
            vec = json.loads(row["vec"])
        except (ValueError, TypeError):
            continue
        scored.append((_cosine(qv, vec), row))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [
        {"doc_id": r["doc_id"], "title": r["title"], "content": r["content"], "score": s}
        for s, r in scored[:limit]
    ]


def hybrid_search(query: str, limit: int = 8) -> list[dict]:
    """Reciprocal-rank fusion of semantic + keyword results."""
    sem = semantic_search(query, limit=limit * 2)
    kw = db.search_chunks(query, limit=limit * 2)

    fused: dict[tuple, dict] = {}

    def key(r):
        return (r["doc_id"], r["content"][:120])

    for rank, r in enumerate(sem):
        k = key(r)
        fused.setdefault(k, {"r": r, "s": 0.0})["s"] += 1.0 / (60 + rank)
    for rank, r in enumerate(kw):
        k = key(r)
        fused.setdefault(k, {"r": r, "s": 0.0})["s"] += 1.0 / (60 + rank)

    ranked = sorted(fused.values(), key=lambda x: x["s"], reverse=True)
    return [x["r"] for x in ranked[:limit]]


def reindex_all() -> int:
    """(Re)build embeddings for every document. Returns the number of chunks indexed."""
    total = 0
    for d in db.list_documents():
        chunks = db.get_chunks(d["id"])
        index_document(d["id"], chunks)
        total += len(chunks)
    return total
