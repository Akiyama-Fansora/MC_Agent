from __future__ import annotations

from pathlib import Path
import os
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.config import AppConfig, ChunkingConfig, EmbeddingConfig, OllamaConfig, PathsConfig, RetrievalConfig  # noqa: E402
from mcagent.llm_profiles import profile_by_id, profiles_payload  # noqa: E402


def make_temp_config(root: Path) -> AppConfig:
    data = root / "data"
    source = data / "crawler_exports"
    source.mkdir(parents=True, exist_ok=True)
    return AppConfig(
        paths=PathsConfig(
            project_root=root,
            source_dir=source,
            db_path=data / "mcagent.sqlite",
            index_path=data / "vector_index.npz",
        ),
        embedding=EmbeddingConfig(),
        chunking=ChunkingConfig(),
        retrieval=RetrievalConfig(),
        ollama=OllamaConfig(),
    )


def assert_equal(name: str, actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"{name}: expected {expected!r}, got {actual!r}")


def assert_true(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name}: {detail}")


def test_deepseek_builtin_reads_local_env_key_without_exposing_secret() -> None:
    previous = os.environ.get("MCAGENT_DEEPSEEK_API_KEY")
    os.environ["MCAGENT_DEEPSEEK_API_KEY"] = "unit-test-deepseek-key"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_temp_config(Path(tmp))
            profile = profile_by_id(config, "deepseek-template")
            payload = profiles_payload(config)
    finally:
        if previous is None:
            os.environ.pop("MCAGENT_DEEPSEEK_API_KEY", None)
        else:
            os.environ["MCAGENT_DEEPSEEK_API_KEY"] = previous

    assert_true("profile_found", isinstance(profile, dict))
    assert_equal("profile_key", profile.get("api_key"), "unit-test-deepseek-key")
    deepseek_public = next(item for item in payload["profiles"] if item["id"] == "deepseek-template")
    assert_equal("public_key_configured", deepseek_public.get("key_configured"), True)
    assert_true("public_key_hidden", "api_key" not in deepseek_public)


def main() -> int:
    test_deepseek_builtin_reads_local_env_key_without_exposing_secret()
    print("LLM PROFILES SCENARIOS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
