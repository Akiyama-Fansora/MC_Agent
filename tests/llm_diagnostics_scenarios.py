from __future__ import annotations

from pathlib import Path
import sys
import urllib.error


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.config import OllamaConfig  # noqa: E402
from mcagent.llm import OpenAICompatibleClient  # noqa: E402
import mcagent.llm as llm_module  # noqa: E402


def assert_true(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name}: {detail}")


def test_llm_url_error_reports_objective_diagnostics_without_api_key() -> None:
    secret = "sk-test-secret"
    client = OpenAICompatibleClient(
        OllamaConfig(base_url="https://example.invalid/v1", model="deepseek-test", timeout_seconds=17),
        api_key=secret,
        provider_label="DeepSeek test",
    )
    original_urlopen = llm_module.urllib.request.urlopen

    def fake_urlopen(_request, timeout=0):  # noqa: ANN001, ANN202
        raise urllib.error.URLError("timed out")

    llm_module.urllib.request.urlopen = fake_urlopen  # type: ignore[assignment]
    try:
        try:
            client.chat([{"role": "user", "content": "ping"}], max_tokens=8)
        except RuntimeError as exc:
            message = str(exc)
        else:
            raise AssertionError("expected RuntimeError")
    finally:
        llm_module.urllib.request.urlopen = original_urlopen  # type: ignore[assignment]

    assert_true("provider_visible", "DeepSeek test" in message, message)
    assert_true("model_visible", "model=deepseek-test" in message, message)
    assert_true("timeout_visible", "timeout=17s" in message, message)
    assert_true("elapsed_visible", "elapsed=" in message and "ms" in message, message)
    assert_true("secret_hidden", secret not in message, message)


def test_llm_http_error_reports_status_without_api_key() -> None:
    secret = "sk-test-secret"
    client = OpenAICompatibleClient(
        OllamaConfig(base_url="https://api.example.test", model="deepseek-test", timeout_seconds=19),
        api_key=secret,
        provider_label="DeepSeek test",
    )
    original_urlopen = llm_module.urllib.request.urlopen

    def fake_urlopen(_request, timeout=0):  # noqa: ANN001, ANN202
        raise urllib.error.HTTPError(
            url="https://api.example.test/chat/completions",
            code=429,
            msg="Too Many Requests",
            hdrs=None,
            fp=None,
        )

    llm_module.urllib.request.urlopen = fake_urlopen  # type: ignore[assignment]
    try:
        try:
            client.chat([{"role": "user", "content": "ping"}], max_tokens=8)
        except RuntimeError as exc:
            message = str(exc)
        else:
            raise AssertionError("expected RuntimeError")
    finally:
        llm_module.urllib.request.urlopen = original_urlopen  # type: ignore[assignment]

    assert_true("status_visible", "HTTP 429" in message, message)
    assert_true("model_visible", "model=deepseek-test" in message, message)
    assert_true("secret_hidden", secret not in message, message)


def main() -> int:
    test_llm_url_error_reports_objective_diagnostics_without_api_key()
    test_llm_http_error_reports_status_without_api_key()
    print("llm_diagnostics_scenarios passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
