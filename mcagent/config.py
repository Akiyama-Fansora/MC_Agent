from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(slots=True)
class PathsConfig:
    project_root: Path
    source_dir: Path
    db_path: Path
    index_path: Path


@dataclass(slots=True)
class EmbeddingConfig:
    provider: str = "hashing_char_ngram"
    dimension: int = 2048
    ngram_min: int = 1
    ngram_max: int = 4
    lowercase: bool = True
    ollama_embed_model: str = "bge-m3"
    ollama_embed_url: str = "http://localhost:11434"


@dataclass(slots=True)
class ChunkingConfig:
    max_chars: int = 1500
    overlap_chars: int = 400


@dataclass(slots=True)
class RetrievalConfig:
    top_k: int = 6
    min_score: float = 0.0


@dataclass(slots=True)
class OllamaConfig:
    base_url: str = "http://localhost:11434/v1"
    model: str = "qwen3-4b-agent-16k:latest"
    temperature: float = 0.2
    timeout_seconds: int = 120


@dataclass(slots=True)
class AppConfig:
    paths: PathsConfig
    embedding: EmbeddingConfig
    chunking: ChunkingConfig
    retrieval: RetrievalConfig
    ollama: OllamaConfig


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _resolve_path(project_root: Path, value: str | os.PathLike[str]) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (project_root / path).resolve()


def _get_section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    return value if isinstance(value, dict) else {}


def load_config(config_path: str | os.PathLike[str] | None = None) -> AppConfig:
    raw_path = config_path or os.environ.get("MCAGENT_CONFIG", "config.json")
    path = Path(raw_path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path

    data = _read_json(path)
    paths_data = _get_section(data, "paths")
    project_root = _resolve_path(
        PROJECT_ROOT,
        paths_data.get("project_root", str(PROJECT_ROOT)),
    )

    source_dir = _resolve_path(
        project_root,
        os.environ.get("MCAGENT_SOURCE_DIR", paths_data.get("source_dir", "data/crawler_exports")),
    )
    db_path = _resolve_path(project_root, paths_data.get("db_path", "data/mcagent.sqlite"))
    index_path = _resolve_path(project_root, paths_data.get("index_path", "data/vector_index.npz"))

    embedding_data = _get_section(data, "embedding")
    chunking_data = _get_section(data, "chunking")
    retrieval_data = _get_section(data, "retrieval")
    ollama_data = _get_section(data, "ollama")

    return AppConfig(
        paths=PathsConfig(
            project_root=project_root,
            source_dir=source_dir,
            db_path=db_path,
            index_path=index_path,
        ),
        embedding=EmbeddingConfig(
            provider=str(embedding_data.get("provider", "hashing_char_ngram")),
            dimension=int(embedding_data.get("dimension", 2048)),
            ngram_min=int(embedding_data.get("ngram_min", 1)),
            ngram_max=int(embedding_data.get("ngram_max", 4)),
            lowercase=bool(embedding_data.get("lowercase", True)),
            ollama_embed_model=str(embedding_data.get("ollama_embed_model", "bge-m3")),
            ollama_embed_url=str(embedding_data.get("ollama_embed_url", "http://localhost:11434")),
        ),
        chunking=ChunkingConfig(
            max_chars=int(chunking_data.get("max_chars", 1500)),
            overlap_chars=int(chunking_data.get("overlap_chars", 400)),
        ),
        retrieval=RetrievalConfig(
            top_k=int(retrieval_data.get("top_k", 6)),
            min_score=float(retrieval_data.get("min_score", 0.0)),
        ),
        ollama=OllamaConfig(
            base_url=os.environ.get(
                "MCAGENT_OLLAMA_BASE_URL",
                str(ollama_data.get("base_url", "http://localhost:11434/v1")),
            ),
            model=os.environ.get(
                "MCAGENT_OLLAMA_MODEL",
                str(ollama_data.get("model", "qwen3-4b-agent-16k:latest")),
            ),
            temperature=float(ollama_data.get("temperature", 0.2)),
            timeout_seconds=int(ollama_data.get("timeout_seconds", 120)),
        ),
    )
