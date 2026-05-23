from __future__ import annotations

from pathlib import Path
import sys
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import mcagent.web_server as web_server  # noqa: E402
from mcagent.agent_message import make_agent_message, message_from_payload  # noqa: E402
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


class SequencedClient:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[list[dict[str, Any]]] = []

    def chat(self, messages: list[dict[str, Any]], *, temperature: float | None = None, max_tokens: int | None = None) -> str:  # noqa: ARG002
        self.calls.append(messages)
        if not self.replies:
            raise AssertionError("fake LLM was called more times than expected")
        return self.replies.pop(0)


def test_agent_message_tuple_and_payload_normalization() -> None:
    message = make_agent_message("User", "你好", "MCAgent", intent="user_chat", conversation_id="s1")
    assert_equal("tuple", message.to_tuple(), ("User", "你好", "MCagent"))
    assert_equal("from_id", message.from_agent_id, "user")
    assert_equal("to_id", message.to_agent_id, "mcagent_rag")
    assert_true("message_id", message.message_id.startswith("msg_"))

    parsed = message_from_payload(
        {"agent_message": {"from_agent": "Crawler", "to_agent": "MCAgent", "content": "本地还缺什么？"}},
        default_to_agent="MCagent",
        default_content="fallback",
    )
    assert_equal("parsed_tuple", parsed.to_tuple(), ("CrawlerAgent", "本地还缺什么？", "MCagent"))


def test_chat_records_user_to_agent_message() -> None:
    tmp = tempfile.TemporaryDirectory()
    fake = SequencedClient(
        [
            '{"tool":"direct_answer","reason":"simple greeting","collection_target":"你好","delivery_target":"human"}',
            '{"proceed":true,"tool":"direct_answer","reason":"ok"}',
            "你好，我是 CrawlerAgent。",
        ]
    )
    original_selector = web_server._selected_llm_client
    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake, "fake")  # type: ignore[assignment]
    try:
        result = web_server._chat_impl(
            make_temp_config(Path(tmp.name)),
            {"agent": "crawler_agent", "question": "你好", "session_id": "message-bus-chat", "model": "fake-model"},
        )
    finally:
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        tmp.cleanup()

    message_steps = [step for step in result.get("trace", []) if step.get("stage") == "message" and step.get("status") == "received"]
    assert_true("message_trace", bool(message_steps))
    assert_equal("message_tuple", tuple(message_steps[0]["detail"]["tuple"]), ("User", "你好", "CrawlerAgent"))
    assert_equal("agent_identity", result.get("agent"), "crawler_agent")
    reply = result.get("agent_message") or {}
    assert_equal("reply_tuple_agents", (reply.get("from_agent"), reply.get("to_agent")), ("CrawlerAgent", "User"))
    assert_true("reply_content", "CrawlerAgent" in str(reply.get("content") or ""))


def test_send_agent_message_dispatches_to_target_agent() -> None:
    tmp = tempfile.TemporaryDirectory()
    fake = SequencedClient(
        [
            '{"tool":"direct_answer","reason":"simple greeting","collection_target":"你好","delivery_target":"human"}',
            '{"proceed":true,"tool":"direct_answer","reason":"ok"}',
            "你好，我是 MCagent。",
        ]
    )
    original_selector = web_server._selected_llm_client
    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake, "fake")  # type: ignore[assignment]
    try:
        result = web_server._send_agent_message(
            make_temp_config(Path(tmp.name)),
            {"session_id": "message-bus-dispatch", "model": "fake-model"},
            make_agent_message("User", "你好", "MCAgent", conversation_id="message-bus-dispatch"),
        )
    finally:
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        tmp.cleanup()

    message_steps = [step for step in result.get("trace", []) if step.get("stage") == "message" and step.get("status") == "received"]
    assert_true("message_trace", bool(message_steps))
    assert_equal("dispatch_agent", result.get("agent"), "mcagent_rag")
    assert_equal("dispatch_tuple", tuple(message_steps[0]["detail"]["tuple"]), ("User", "你好", "MCagent"))
    reply = result.get("agent_message") or {}
    assert_equal("dispatch_reply_agents", (reply.get("from_agent"), reply.get("to_agent")), ("MCagent", "User"))
    assert_true("dispatch_reply_to", bool(reply.get("reply_to")))


def main() -> int:
    test_agent_message_tuple_and_payload_normalization()
    test_chat_records_user_to_agent_message()
    test_send_agent_message_dispatches_to_target_agent()
    print("agent_message_bus_scenarios passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
