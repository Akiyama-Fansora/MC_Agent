from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.config import load_config  # noqa: E402


def assert_equal(name: str, actual, expected) -> None:
    if actual != expected:
        raise AssertionError(f"{name}: expected {expected!r}, got {actual!r}")


def test_load_config_sanitizes_malformed_values() -> None:
    with tempfile.TemporaryDirectory(prefix="mcagent-config-") as tmp:
        root = Path(tmp)
        source_dir = root / "crawler_exports"
        source_dir.mkdir(parents=True, exist_ok=True)
        config_path = root / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "paths": {
                        "project_root": str(root),
                        "source_dir": "crawler_exports",
                        "db_path": "data/custom.sqlite",
                        "index_path": "data/custom-index.npz",
                    },
                    "embedding": {
                        "provider": "hashing_char_ngram",
                        "dimension": "many",
                        "ngram_min": "3",
                        "ngram_max": "1",
                        "lowercase": "false",
                    },
                    "chunking": {
                        "max_chars": "0",
                        "overlap_chars": "-5",
                    },
                    "retrieval": {
                        "top_k": "0",
                        "min_score": "0.25",
                    },
                    "ollama": {
                        "base_url": "https://example.invalid/v1",
                        "model": "unit-test-model",
                        "temperature": "not-a-number",
                        "timeout_seconds": "0",
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        config = load_config(config_path)

    assert_equal("project_root", config.paths.project_root, root)
    assert_equal("source_dir", config.paths.source_dir, source_dir)
    assert_equal("db_path", config.paths.db_path, root / "data/custom.sqlite")
    assert_equal("index_path", config.paths.index_path, root / "data/custom-index.npz")
    assert_equal("dimension", config.embedding.dimension, 2048)
    assert_equal("ngram_min", config.embedding.ngram_min, 3)
    assert_equal("ngram_max", config.embedding.ngram_max, 3)
    assert_equal("lowercase", config.embedding.lowercase, False)
    assert_equal("max_chars", config.chunking.max_chars, 200)
    assert_equal("overlap_chars", config.chunking.overlap_chars, 0)
    assert_equal("top_k", config.retrieval.top_k, 1)
    assert_equal("min_score", config.retrieval.min_score, 0.25)
    assert_equal("base_url", config.ollama.base_url, "https://example.invalid/v1")
    assert_equal("model", config.ollama.model, "unit-test-model")
    assert_equal("temperature", config.ollama.temperature, 0.2)
    assert_equal("timeout_seconds", config.ollama.timeout_seconds, 1)


def main() -> int:
    test_load_config_sanitizes_malformed_values()
    print("config_load_scenarios passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
