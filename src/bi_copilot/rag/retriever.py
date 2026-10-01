"""Retrieval over the business knowledge base.

Sources: knowledge_base/*.md (chunked by '## ' section) and every metric in
the catalog (so definitions are retrievable by synonym). The default index is
TF-IDF vectors with cosine similarity, which needs no API key and runs
anywhere. If an embedding provider is configured, dense vectors are added and
scores are blended (hybrid retrieval).

eval/ground_truth is never indexed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, Optional

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

from ..config import settings
from ..semantic.catalog import Catalog, get_catalog


@dataclass
class Chunk:
    id: str
    source: str
    title: str
    text: str

    def cite(self) -> str:
        return f"{self.source} › {self.title}"


@dataclass
class Hit:
    chunk: Chunk
    score: float


def load_chunks(kb_dir: Path | None = None, catalog: Catalog | None = None) -> list[Chunk]:
    kb_dir = Path(kb_dir or settings.knowledge_dir)
    chunks: list[Chunk] = []
    for f in sorted(kb_dir.glob("*.md")):
        if "ground_truth" in f.parts:
            continue
        text = f.read_text()
        doc_title = (re.search(r"^# (.+)$", text, re.M) or [None, f.stem])[1]
        for sec in re.split(r"(?m)^## ", text)[1:]:
            title, _, body = sec.partition("\n")
            chunks.append(Chunk(id=f"{f.stem}#{len(chunks)}", source=f"{f.stem}.md", title=title.strip(),
                                text=f"{doc_title}. {title.strip()}. {body.strip()}"))
    cat = catalog or get_catalog()
    for m in cat.visible_metrics():
        body = (f"Metric: {m.label} ({m.name}). Definition: {m.definition} Owner: {m.owner or 'n/a'}. "
                f"Grain: {m.grain or 'derived'}. Also called: {', '.join(m.synonyms)}.")
        chunks.append(Chunk(id=f"metric:{m.name}", source="metric_catalog", title=m.label, text=body))
    for concept, why in cat.unsupported.items():
        chunks.append(Chunk(id=f"unsupported:{concept}", source="metric_catalog", title=f"Not available: {concept}",
                            text=f"{concept} is not available. {why}"))
    return chunks


class Retriever:
    def __init__(self, chunks: list[Chunk] | None = None, embed: Optional[Callable[[list[str]], np.ndarray]] = None):
        self.chunks = chunks or load_chunks()
        self.vec = TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True, stop_words="english", min_df=1)
        self.matrix = self.vec.fit_transform([c.text for c in self.chunks])
        self.embed = embed
        self.dense = None
        if embed is not None:
            try:
                d = embed([c.text for c in self.chunks])
                self.dense = d / np.linalg.norm(d, axis=1, keepdims=True)
            except Exception:  # noqa: BLE001 - fall back to sparse only
                self.embed, self.dense = None, None

    @property
    def mode(self) -> str:
        return "hybrid (tf-idf + dense embeddings)" if self.dense is not None else "tf-idf vector search"

    def search(self, query: str, k: int = 4, min_score: float = 0.05) -> list[Hit]:
        qv = self.vec.transform([query])
        scores = (self.matrix @ qv.T).toarray().ravel()
        if self.dense is not None:
            e = self.embed([query])[0]
            e = e / np.linalg.norm(e)
            scores = 0.5 * scores + 0.5 * (self.dense @ e)
        order = np.argsort(-scores)[:k]
        return [Hit(self.chunks[i], float(scores[i])) for i in order if scores[i] >= min_score]


@lru_cache(maxsize=1)
def get_retriever() -> Retriever:
    embed = None
    try:
        from ..llm.providers import get_embedder
        embed = get_embedder()
    except Exception:  # noqa: BLE001
        embed = None
    return Retriever(embed=embed)
