from __future__ import annotations

from pathlib import Path
import sys
import tempfile

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.config import (  # noqa: E402
    AppConfig,
    ChunkingConfig,
    EmbeddingConfig,
    OllamaConfig,
    PathsConfig,
    RetrievalConfig,
)
from mcagent.fastapi_app import create_app  # noqa: E402
import mcagent.web_server as web_server  # noqa: E402


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
        ollama=OllamaConfig(timeout_seconds=1),
    )


def assert_true(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name}: {detail}")


class SequencedClient:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)

    def chat(self, messages, *, temperature=None, max_tokens=None):  # noqa: ANN001, ANN201, ARG002
        if not self.replies:
            raise AssertionError("fake LLM was called more times than expected")
        return self.replies.pop(0)


def test_fastapi_core_routes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client = TestClient(create_app(make_temp_config(Path(tmp))))
        health = client.get("/api/health")
        assert_true("health_ok", health.status_code == 200 and health.json().get("backend") == "fastapi")

        agents = client.get("/api/agents")
        assert_true("agents_ok", agents.status_code == 200)
        names = [item.get("id") for item in agents.json().get("agents", [])]
        assert_true("mcagent_present", "mcagent_rag" in names)
        assert_true("crawler_present", "crawler_agent" in names)

        tools = client.get("/api/agents/mcagent_rag/tools")
        tools_json = tools.json()
        route_tool_names = [item.get("name") for item in tools_json.get("route_tools", [])]
        assert_true("agent_tools_ok", tools.status_code == 200)
        assert_true("agent_tools_include_rag", "local_rag_search" in route_tool_names)
        assert_true("agent_tools_catalog", "Available tools" in tools_json.get("catalog", ""))

        crawler_tools = client.get("/api/agents/crawler_agent/tools")
        crawler_tools_json = crawler_tools.json()
        crawler_route_names = [item.get("name") for item in crawler_tools_json.get("route_tools", [])]
        assert_true("crawler_tools_ok", crawler_tools.status_code == 200)
        assert_true("crawler_tools_include_temporary_extract", "temporary_extract" in crawler_route_names)
        assert_true("crawler_tools_include_delegate", "delegate_crawler" in crawler_route_names)

        status = client.get("/api/status")
        assert_true("status_ok", status.status_code == 200 and "database" in status.json())

        session = client.post("/api/session", json={"session_id": "fastapi-test"})
        assert_true("session_ok", session.status_code == 200 and session.json().get("history") == [])

        context = client.post("/api/session/context", json={"session_id": "fastapi-test", "agent": "mcagent_rag"})
        context_json = context.json()
        assert_true("context_ok", context.status_code == 200 and context_json.get("session_id") == "fastapi-test")
        assert_true("context_summary", isinstance(context_json.get("summary"), dict))
        assert_true("context_turn_count", context_json.get("turn_count") == 0)

        deleted = client.post("/api/session/delete", json={"session_id": "fastapi-test"})
        assert_true("session_delete_ok", deleted.status_code == 200 and deleted.json().get("session_id") == "fastapi-test")


def test_fastapi_sse_chat_shape() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client = TestClient(create_app(make_temp_config(Path(tmp))))
        with client.stream("POST", "/api/chat/stream", json={"question": "", "session_id": "sse-test"}) as response:
            text = "".join(response.iter_text())
        assert_true("sse_status", response.status_code == 200)
        assert_true("sse_response_event", "event: response" in text)
        assert_true("sse_agent_message", '"agent_message"' in text)
        assert_true("sse_done_event", "event: done" in text)

        with client.stream("POST", "/api/chat/stream", json={"question": "状态", "session_id": "sse-status-test"}) as response:
            status_text = "".join(response.iter_text())
        assert_true("sse_status_command_status", response.status_code == 200)
        assert_true("sse_status_command_response", "event: response" in status_text, status_text[:500])
        assert_true("sse_status_command_done", "event: done" in status_text, status_text[:500])


def test_fastapi_agent_message_endpoint_dispatches() -> None:
    fake = SequencedClient(
        [
            '{"tool":"direct_answer","reason":"simple greeting","collection_target":"你好","delivery_target":"human"}',
            '{"proceed":true,"tool":"direct_answer","reason":"ok"}',
            '{"missing_side_effect":false,"action":"allow","reason":"simple greeting has no required side effect"}',
            "你好，我是 CrawlerAgent。",
        ]
    )
    original_selector = web_server._selected_llm_client
    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake, "fake")  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            client = TestClient(create_app(make_temp_config(Path(tmp))))
            response = client.post(
                "/api/agent-message",
                json={"from_agent": "User", "to_agent": "CrawlerAgent", "content": "你好", "session_id": "fastapi-message"},
            )
    finally:
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
    assert_true("agent_message_status", response.status_code == 200, response.text)
    body = response.json()
    assert_true("agent_message_agent", body.get("agent") == "crawler_agent")
    reply = body.get("agent_message") or {}
    assert_true("agent_message_reply", reply.get("from_agent") == "CrawlerAgent" and reply.get("to_agent") == "User")
    traces = body.get("trace") or []
    assert_true("agent_message_trace", any(step.get("stage") == "message" and step.get("status") == "received" for step in traces))


def main() -> int:
    test_fastapi_core_routes()
    test_fastapi_sse_chat_shape()
    test_fastapi_agent_message_endpoint_dispatches()
    print("FASTAPI BACKEND SCENARIOS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
