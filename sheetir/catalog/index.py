"""Search over catalog cards (design §6.7): BM25 on unaccented words, plus cosine similarity of embeddings when a local
embedding model is available (embedding.model_path, loaded with sentence-transformers, never downloaded):

    score = α·cosine + (1 − α)·bm25 / max(bm25)        α = embedding.hybrid_alpha
"""

from __future__ import annotations

import re
from pathlib import Path

from rank_bm25 import BM25Okapi
from unidecode import unidecode


def tokenize(text: str) -> list[str]:
    """Lowercase unaccented words: 'Chi nhánh' and 'chi nhanh' match. Identifiers split at underscores."""
    return [t for t in re.split(r"[^a-z0-9]+", unidecode(text or "").lower()) if t]


class Embedder:
    """A local sentence-transformers model, or nothing. Loading never reaches the network (local_files_only)."""

    _cache: dict[str, object] = {}

    def __init__(self, model_path: str | Path | None):
        self.model = None
        p = Path(model_path) if model_path else None
        if p is None or not p.exists():
            return
        key = str(p.resolve())
        if key in Embedder._cache:
            self.model = Embedder._cache[key]
            return
        try:
            from sentence_transformers import SentenceTransformer
            self.model = SentenceTransformer(str(p), local_files_only=True)
            Embedder._cache[key] = self.model
        except Exception:  # noqa: BLE001 - no embeddings: BM25 alone
            self.model = None

    def encode(self, texts: list[str]):
        return self.model.encode(texts, normalize_embeddings=True) if self.model is not None else None


class CardIndex:
    def __init__(self, card_ids: list[str], texts: list[str], vectors=None, embedder: Embedder | None = None,
                 alpha: float = 0.6):
        self.card_ids, self.alpha = card_ids, alpha
        self.docs = [tokenize(t) for t in texts]
        self.bm25 = BM25Okapi(self.docs) if self.docs and any(self.docs) else None
        self.vectors, self.embedder = vectors, embedder

    def search(self, query: str, k: int = 8) -> list[tuple[str, float]]:
        """The k best cards for `query`, as (card_id, score in 0..1), best first. A query that matches nothing still
        returns k cards (all scored 0, in catalog order), so the caller always has something to describe."""
        if not self.card_ids:
            return []
        q = tokenize(query)
        scores = [0.0] * len(self.card_ids)
        if self.bm25 is not None and q:
            raw = list(self.bm25.get_scores(q))
            # BM25's idf is negative for words in more than half the documents (small catalogs): count plain overlap
            if max(raw) <= 0:
                raw = [len(set(q) & set(d)) / (len(set(q)) or 1) for d in self.docs]
            top = max(raw) or 1.0
            scores = [max(0.0, s) / top for s in raw]
        if self.vectors is not None and self.embedder is not None and self.embedder.model is not None:
            qv = self.embedder.encode([query])[0]
            cos = (self.vectors @ qv).tolist()
            scores = [self.alpha * c + (1 - self.alpha) * s for c, s in zip(cos, scores)]
        order = sorted(range(len(scores)), key=lambda i: -scores[i])[:k]
        return [(self.card_ids[i], round(float(scores[i]), 4)) for i in order]
