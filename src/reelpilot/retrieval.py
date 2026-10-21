"""BM25 + multimodal dense retrieval, fused only over observed positive ranks."""
from __future__ import annotations

from collections import Counter
import math
import re
from pathlib import Path
from typing import Callable, Literal

import numpy as np

from .library import Library
from .schema import Hit


def tokenize(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", text.lower())
    out = []
    for word in words:
        if "\u4e00" <= word[0] <= "\u9fff":
            out.extend(word[i:i+2] for i in range(max(1, len(word)-1)))
        else:
            out.append(word)
    return out


class Retriever:
    def __init__(self, library: Library, query_encoder: Callable | None = None, *, view="image"):
        self.library = library
        self.rows = list(library.segments.values())
        self.encoder = query_encoder
        self.terms = [Counter(tokenize(s.transcript)) for s in self.rows]
        self.lengths = np.array([sum(t.values()) for t in self.terms], dtype=float)
        self.mean_length = max(1.0, float(self.lengths.mean())) if self.rows else 1.0
        df = Counter(term for row in self.terms for term in row)
        self.idf = {t: math.log(1+(len(self.rows)-n+0.5)/(n+0.5)) for t, n in df.items()}
        self.view = view
        self.views = {}
        for kind in ["joint", "image", "text"]:
            stem = "vectors" if kind == "joint" else "vectors_"+kind
            vector_path = library.root / (stem+".npz")
            if not vector_path.exists():
                continue
            import json
            meta = json.loads((library.root / (stem+".json")).read_text(encoding="utf-8"))
            if meta["collection_hash"] != library.hash:
                raise ValueError("dense index belongs to a different media collection")
            data = np.load(vector_path, allow_pickle=False)
            if list(data["ids"]) != [s.id for s in self.rows]:
                raise ValueError("dense index row order mismatch")
            vectors = data["vectors"].astype(np.float32)
            if not np.isfinite(vectors).all() or len(vectors) != len(self.rows):
                raise ValueError("invalid dense vectors")
            self.views[kind] = (vectors, meta)

    def lexical(self, query: str) -> np.ndarray:
        scores = np.zeros(len(self.rows))
        for term in set(tokenize(query)):
            freq = np.array([d.get(term, 0) for d in self.terms], dtype=float)
            scores += self.idf.get(term, 0) * freq * 2.2 / (
                freq + 1.2 * (0.25+0.75*self.lengths/self.mean_length))
        return scores

    def search(self, query: str, *, mode="adaptive", limit=12,
               asset_ids: list[str] | None = None, view: str | None = None) -> list[Hit]:
        chosen_view = view or self.view
        if chosen_view not in {"image", "text", "joint"}:
            raise ValueError("unknown embedding view")
        if mode == "adaptive":
            mode = "hybrid" if chosen_view == "text" else "dense"
        if mode not in {"lexical", "dense", "hybrid"}:
            raise ValueError("unknown retrieval mode")
        allowed = [i for i, row in enumerate(self.rows) if not asset_ids or row.asset_id in asset_ids]
        if chosen_view == "text":
            allowed = [i for i in allowed if self.rows[i].transcript.strip()]
        elif mode != "lexical":
            allowed = [i for i in allowed if self.rows[i].visual_usable]
        lexical = self.lexical(query)
        dense = None
        rankings = []
        if mode in {"lexical", "hybrid"}:
            rankings.append(sorted((i for i in allowed if lexical[i] > 0), key=lambda i: (-lexical[i], i)))
        if mode in {"dense", "hybrid"}:
            if chosen_view not in self.views or self.encoder is None:
                raise RuntimeError("dense retrieval requires a real index and its matching query encoder")
            vectors, vector_meta = self.views[chosen_view]
            if getattr(self.encoder, "fingerprint", None) != vector_meta.get("fingerprint"):
                raise ValueError("query encoder fingerprint differs from the indexed embedding space")
            vector = np.asarray(self.encoder(query), dtype=np.float32).reshape(-1)
            norm = float(np.linalg.norm(vector))
            if not np.isfinite(vector).all() or norm <= 0 or vector.shape[0] != vectors.shape[1]:
                raise ValueError("query embedding is invalid or uses a different embedding space")
            dense = vectors @ (vector/norm)
            rankings.append(sorted(allowed, key=lambda i: (-dense[i], i)))
        fused = {}
        for ranking in rankings:
            for rank, i in enumerate(ranking[:100], 1):
                fused[i] = fused.get(i, 0) + 1/(60+rank)
        selected = sorted(fused, key=lambda i: (-fused[i], i))[:max(0, min(limit, 100))]
        return [Hit(segment=self.rows[i], score=fused[i], lexical_score=float(lexical[i]),
                    dense_score=float(dense[i]) if dense is not None else None,
                    evidence=(["transcript"] if lexical[i] > 0 and mode != "dense" else []) +
                             (["embedding:"+chosen_view] if dense is not None else [])) for i in selected]

