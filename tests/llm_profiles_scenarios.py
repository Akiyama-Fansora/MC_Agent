from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.config import load_config  # noqa: E402
import mcagent.llm_profiles as llm_profiles  # noqa: E402


def assert_equal(name: str, actual, expected) -> None:
    if actual != expected:
        raise AssertionError(f"{name}: expected {expected!r}, got {actual!r}")


def test_profile_timeout_normalization() -> None:
    existing = {"timeout_seconds": 75}
    base = {"id": "test", "base_url": "http://localhost:11434/v1", "model": "test-model"}

    assert_equal(
        "malformed_falls_back_to_existing",
        llm_profiles._sanitize_profile({**base, "timeout_seconds": "auto"}, existing=existing)["timeout_seconds"],
        75,
    )
    assert_equal(
        "non_finite_falls_back_to_existing",
        llm_profiles._sanitize_profile({**base, "timeout_seconds": float("nan")}, existing=existing)["timeout_seconds"],
        75,
    )
    assert_equal("minimum", llm_profiles._sanitize_profile({**base, "timeout_seconds": -1})["timeout_seconds"], 15)
    assert_equal("maximum", llm_profiles._sanitize_profile({**base, "timeout_seconds": 9999})["timeout_seconds"], 900)
    assert_equal("valid_string", llm_profiles._sanitize_profile({**base, "timeout_seconds": "45"})["timeout_seconds"], 45)


def test_malformed_persisted_timeout_does_not_break_profile_loading() -> None:
    config = load_config()
    original_path = llm_profiles.PROFILE_PATH
    with tempfile.TemporaryDirectory() as tmp:
        profile_path = Path(tmp) / "llm_profiles.json"
        profile_path.write_text(
            json.dumps(
                {
                    "profiles": [
                        {
                            "id": "persisted-test",
                            "name": "Persisted test",
                            "provider": "openai-compatible",
                            "base_url": "https://example.test",
                            "model": "test-model",
                            "timeout_seconds": "later",
                        }
                    ],
                    "assignments": {"mcagent_rag": "persisted-test"},
                }
            ),
            encoding="utf-8",
        )
        llm_profiles.PROFILE_PATH = profile_path
        try:
            payload = llm_profiles.profiles_payload(config)
        finally:
            llm_profiles.PROFILE_PATH = original_path

    profile = next(item for item in payload["profiles"] if item["id"] == "persisted-test")
    assert_equal("persisted_timeout_default", profile["timeout_seconds"], 180)
    assert_equal("persisted_assignment", payload["assignments"]["mcagent_rag"], "persisted-test")


def test_malformed_api_timeout_is_saved_as_default() -> None:
    config = load_config()
    original_path = llm_profiles.PROFILE_PATH
    with tempfile.TemporaryDirectory() as tmp:
        profile_path = Path(tmp) / "llm_profiles.json"
        llm_profiles.PROFILE_PATH = profile_path
        try:
            payload = llm_profiles.save_profiles_payload(
                config,
                {
                    "profiles": [
                        {
                            "id": "api-test",
                            "name": "API test",
                            "provider": "openai-compatible",
                            "base_url": "https://example.test",
                            "model": "test-model",
                            "timeout_seconds": {"unexpected": True},
                        }
                    ],
                    "assignments": {"mcagent_rag": "api-test", "crawler_agent": "api-test"},
                },
            )
            stored = json.loads(profile_path.read_text(encoding="utf-8"))
        finally:
            llm_profiles.PROFILE_PATH = original_path

    profile = next(item for item in payload["profiles"] if item["id"] == "api-test")
    assert_equal("api_timeout_default", profile["timeout_seconds"], 180)
    assert_equal("stored_timeout_default", stored["profiles"][0]["timeout_seconds"], 180)
    assert_equal("mcagent_assignment", payload["assignments"]["mcagent_rag"], "api-test")
    assert_equal("crawler_assignment", payload["assignments"]["crawler_agent"], "api-test")


if __name__ == "__main__":
    test_profile_timeout_normalization()
    test_malformed_persisted_timeout_does_not_break_profile_loading()
    test_malformed_api_timeout_is_saved_as_default()
    print("llm_profiles_scenarios passed")
