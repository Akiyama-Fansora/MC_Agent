from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re
import urllib.request
from typing import Iterable

from .config import EmbeddingConfig


def _require_numpy():
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError(
            "NumPy is required for the local .npz vector index. "
            "Install it with: pip install -r requirements.txt"
        ) from exc
    return np


@dataclass(slots=True)
class OllamaEmbedder:
    model: str = "bge-m3"
    base_url: str = "http://localhost:11434"
    dimension: int = 0

    provider_name: str = "ollama"

    def _embed_one(self, text: str) -> list[float]:
        req = urllib.request.Request(
            f"{self.base_url.rstrip('/')}/api/embeddings",
            data=json.dumps({"model": self.model, "prompt": text}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read())
        embedding = payload.get("embedding")
        if not isinstance(embedding, list) or not embedding:
            raise RuntimeError(f"Ollama embedding model {self.model!r} returned no embedding.")
        return [float(value) for value in embedding]

    def embed(self, texts: list[str]):
        np = _require_numpy()
        if not texts:
            width = self.dimension or 0
            return np.zeros((0, width), dtype=np.float32)

        embeddings = [self._embed_one(text) for text in texts]
        width = len(embeddings[0])
        if width <= 0:
            raise RuntimeError(f"Ollama embedding model {self.model!r} returned an empty vector.")
        for embedding in embeddings:
            if len(embedding) != width:
                raise RuntimeError(
                    f"Ollama embedding model {self.model!r} returned inconsistent dimensions "
                    f"({len(embedding)} != {width})."
                )
        self.dimension = width
        vectors = np.asarray(embeddings, dtype=np.float32)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        vectors = np.divide(vectors, norms, out=np.zeros_like(vectors), where=norms > 0)
        return vectors


@dataclass(slots=True)
class HashingCharNgramEmbedder:
    dimension: int = 2048
    ngram_min: int = 1
    ngram_max: int = 4
    lowercase: bool = True

    provider_name: str = "hashing_char_ngram"

    def _normalize(self, text: str) -> str:
        if self.lowercase:
            text = text.lower()
        return re.sub(r"\s+", " ", text).strip()

    def _ngrams(self, text: str) -> Iterable[str]:
        normalized = self._normalize(text)
        if not normalized:
            return []
        grams: list[str] = []
        for ngram_size in range(self.ngram_min, self.ngram_max + 1):
            if len(normalized) < ngram_size:
                continue
            grams.extend(normalized[idx : idx + ngram_size] for idx in range(len(normalized) - ngram_size + 1))
        return grams

    def embed(self, texts: list[str]):
        np = _require_numpy()
        vectors = np.zeros((len(texts), self.dimension), dtype=np.float32)
        for row, text in enumerate(texts):
            total = 0
            for gram in self._ngrams(text):
                digest = hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest()
                value = int.from_bytes(digest, byteorder="little", signed=False)
                index = value % self.dimension
                vectors[row, index] += 1.0
                total += 1
            if total:
                norm = float(np.linalg.norm(vectors[row]))
                if norm > 0:
                    vectors[row] /= norm
            else:
                vectors[row, 0] = 1.0 / math.sqrt(1.0)
        return vectors


def make_embedder(config: EmbeddingConfig):
    if config.provider == "ollama":
        return OllamaEmbedder(
            model=getattr(config, 'ollama_embed_model', 'bge-m3'),
            base_url=getattr(config, 'ollama_embed_url', 'http://localhost:11434'),
        )
    if config.provider != "hashing_char_ngram":
        raise ValueError(
            f"Unsupported embedding provider: {config.provider}. "
            "Supported: hashing_char_ngram, ollama"
        )
    return HashingCharNgramEmbedder(
        dimension=config.dimension,
        ngram_min=config.ngram_min,
        ngram_max=config.ngram_max,
        lowercase=config.lowercase,
    )
