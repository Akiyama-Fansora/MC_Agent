from __future__ import annotations

from pathlib import Path
import sys
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import mcagent.web_server as web_server  # noqa: E402
from mcagent.config import AppConfig, ChunkingConfig, EmbeddingConfig, OllamaConfig, PathsConfig, RetrievalConfig  # noqa: E402


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
        ollama=OllamaConfig(model="fake-model"),
    )


def assert_equal(name: str, actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"{name}: expected {expected!r}, got {actual!r}")


def assert_true(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name}: {detail}")


def test_direct_crawler_no_save_url_uses_temporary_extract_boundary() -> None:
    question = "总结一下 https://example.com 的内容给我，不用保存到本地"
    assert_true(
        "temporary_boundary",
        web_server._should_use_temporary_extract_without_persistence("crawler_agent", question, question, "human"),
    )
    assert_true(
        "mcagent_not_forced",
        not web_server._should_use_temporary_extract_without_persistence("mcagent_rag", question, question, "human"),
    )
    assert_true(
        "rag_delivery_not_forced",
        not web_server._should_use_temporary_extract_without_persistence("crawler_agent", question, question, "MCagent/RAG"),
    )
    neutral_url_question = "总结一下 https://example.com 的内容给我"
    assert_true(
        "plain_url_summary_is_temporary_by_default",
        web_server._should_use_temporary_extract_without_persistence("crawler_agent", neutral_url_question, neutral_url_question, "human"),
    )
    save_question = r"读取 https://example.com 的内容并保存到 C:\tmp\example.md"
    assert_true(
        "explicit_save_stays_persistent",
        not web_server._should_use_temporary_extract_without_persistence("crawler_agent", save_question, save_question, "human"),
    )


class FakeClient:
    def chat(self, messages: list[dict[str, Any]], *, temperature: float, max_tokens: int) -> str:  # noqa: ARG002
        return '{"handoff_brief":"调用关系：MCagent 将用户请求转交给 CrawlerAgent。","reason":"fake"}'


def test_direct_user_handoff_brief_rejects_wrong_mcagent_identity() -> None:
    original_selector = web_server._selected_llm_client
    web_server._selected_llm_client = lambda *_args, **_kwargs: (FakeClient(), "fake")  # type: ignore[assignment]
    try:
        brief, reason = web_server._build_delegate_handoff_brief(
            object(),  # type: ignore[arg-type]
            model="fake",
            original_question="Crawler 直接采集公开网页",
            collection_target="采集公开网页",
            session_summary={},
            requested_by="user",
            delivery_target="human",
        )
    finally:
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]

    assert_true("uses_user_identity", "Requested by: user" in brief or "From: user" in brief)
    assert_true("no_wrong_mcagent_transfer", "MCagent 将用户请求转交" not in brief)
    assert_equal("reason", reason, "LLM handoff brief conflicted with requested_by=user; used identity-safe fallback.")


class SequencedClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls: list[list[dict[str, Any]]] = []

    def chat(self, messages: list[dict[str, Any]], *, temperature: float, max_tokens: int) -> str:  # noqa: ARG002
        self.calls.append(messages)
        if not self.responses:
            return "ok"
        return self.responses.pop(0)


def test_direct_crawler_delegate_choice_is_corrected_to_temporary_extract() -> None:
    tmp = tempfile.TemporaryDirectory()
    fake_client = SequencedClient(
        [
            '{"tool":"delegate_crawler","reason":"mistaken persistent route","collection_target":"总结 https://example.com 页面内容","delivery_target":"human"}',
            '{"proceed":true,"tool":"delegate_crawler","reason":"confirm mistaken route"}',
            '{"proceed":true,"tool":"temporary_extract","reason":"temporary extraction is allowed"}',
            "Example page summary.",
        ]
    )
    original_selector = web_server._selected_llm_client
    original_fetch_text = web_server.CrawlerTemporaryExtractService.fetch_text
    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake_client, "fake")  # type: ignore[assignment]
    web_server.CrawlerTemporaryExtractService.fetch_text = (  # type: ignore[assignment]
        lambda self, url, *, fetch=None: ("Example", "Example body text. " * 20, "text/html", 200)
    )
    try:
        result = web_server._chat_impl(
            make_temp_config(Path(tmp.name)),
            {
                "agent": "crawler_agent",
                "question": "总结一下 https://example.com 的内容给我，不用保存到本地",
                "session_id": "direct-crawler-side-effect-test",
                "model": "fake-model",
            },
        )
    finally:
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        web_server.CrawlerTemporaryExtractService.fetch_text = original_fetch_text  # type: ignore[assignment]
        tmp.cleanup()

    statuses = [(step["stage"], step["status"]) for step in result.get("trace", [])]
    assert_true("boundary_trace", ("decide", "side_effect_boundary_corrected") in statuses)
    assert_true("temporary_result", result.get("temporary_extract", {}).get("saved_to_local") is False)
    assert_equal("agent_identity", result.get("agent"), "crawler_agent")
    assert_true("no_background_job", "job" not in result)


if __name__ == "__main__":
    test_direct_crawler_no_save_url_uses_temporary_extract_boundary()
    test_direct_user_handoff_brief_rejects_wrong_mcagent_identity()
    test_direct_crawler_delegate_choice_is_corrected_to_temporary_extract()
    print("web_server_side_effect_guard_scenarios passed")
