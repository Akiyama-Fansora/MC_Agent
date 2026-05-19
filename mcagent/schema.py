from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class RawDocument:
    source_ref: str
    source_path: Path
    title: str
    text: str
    url: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TextChunk:
    document_source_ref: str
    chunk_index: int
    text: str
    start_char: int
    end_char: int
    token_estimate: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SearchResult:
    rank: int
    score: float
    chunk_id: int
    document_id: int
    chunk_index: int
    title: str
    source_path: str
    url: str | None
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)
