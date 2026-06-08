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
            '{"proceed":true,"tool":"direct_answer","goal":"greet user","reason":"simple greeting"}',
            '{"missing_side_effect":false,"action":"allow","reason":"simple greeting has no required side effect"}',
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
            '{"proceed":true,"tool":"direct_answer","goal":"greet user","reason":"simple greeting"}',
            '{"missing_side_effect":false,"action":"allow","reason":"simple greeting has no required side effect"}',
            "你好，我是 MCagent。",
        ]
    )
    original_selector = web_server._selected_llm_client
    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake, "fake")  # type: ignore[assignment]
    try:
        result = web_server._send_agent_message(
            make_temp_config(Path(tmp.name)),
            {"session_id": "message-bus-dispatch", "model": "fake-model"},
            from_agent="User",
            content="你好",
            to_agent="MCAgent",
            conversation_id="message-bus-dispatch",
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


def test_message_bus_api_is_single_from_content_to_primitive() -> None:
    import inspect

    signature = inspect.signature(web_server._send_agent_message)
    params = signature.parameters
    for name in ("config", "payload", "from_agent", "content", "to_agent"):
        assert_true(f"required_param_{name}", name in params)
    assert_true("keyword_only_from", params["from_agent"].kind is inspect.Parameter.KEYWORD_ONLY)
    assert_true("keyword_only_content", params["content"].kind is inspect.Parameter.KEYWORD_ONLY)
    assert_true("keyword_only_to", params["to_agent"].kind is inspect.Parameter.KEYWORD_ONLY)
    assert_true("no_message_param", "message" not in params)

    source = (ROOT / "mcagent" / "web_server.py").read_text(encoding="utf-8")
    assert_true("chat_uses_message_bus", "executor.submit(_send_agent_message, config, payload, **fields)" in source)
    assert_true("stream_uses_message_bus", "_send_agent_message(config, payload, emit=emit, **_user_message_fields(payload))" in source)
    assert_true("no_object_bus_call", "_send_agent_message(config, payload, message" not in source)
    assert_true("no_legacy_crawler_start_api", "/api/jobs/start-crawler" not in source)
    assert_true("no_legacy_crawler_request_wrapper", "def _send_crawler_collection_request" not in source)
    assert_true("collection_request_not_runtime_forced", "agent_message_contract" not in source)
    assert_true("collection_request_only_context", "collection_request_received_for_agent_decision" in source)
    assert_true("direct_answer_review_does_not_force_delegate", "direct_answer_corrected_to_delegation" not in source)
    assert_true("post_answer_review_does_not_force_delegate", "post_answer_route_completeness_gap_not_executed" in source)
    assert_true("protocol_review_blocks_unselected_delegate", "protocol_violation_side_effect_not_executed" in source)
    start = source.index("def _start_crawler_job_from_crawler_tool")
    end = source.index("\ndef _fallback_delegate_handoff_brief", start)
    job_start_body = source[start:end]
    assert_true("crawler_tool_requires_received_message", "_received_agent_message_for_tool(" in job_start_body)
    assert_true("crawler_tool_does_not_forge_message", "make_agent_message(" not in job_start_body)


def test_production_entries_do_not_bypass_message_bus_runtime() -> None:
    web_source = (ROOT / "mcagent" / "web_server.py").read_text(encoding="utf-8")
    fastapi_source = (ROOT / "mcagent" / "fastapi_app.py").read_text(encoding="utf-8")

    chat_impl_refs = [
        line.strip()
        for line in web_source.splitlines()
        if "_chat_impl(" in line
    ]
    assert_equal(
        "web_server_chat_impl_refs",
        chat_impl_refs,
        [
            "return _chat_impl(config, payload, emit=emit)",
            "def _chat_impl(config: AppConfig, payload: dict[str, Any], emit: Any | None = None) -> dict[str, Any]:",
        ],
    )
    send_start = web_source.index("def _send_agent_message")
    send_end = web_source.index("\ndef _deliver_agent_message", send_start)
    send_body = web_source[send_start:send_end]
    assert_true("message_bus_enters_langgraph", "dispatch_agent_message_graph(" in send_body)
    assert_true("message_bus_uses_agent_delivery_node", "agent_delivery=_deliver_agent_message" in send_body)
    delivery_start = web_source.index("def _deliver_agent_message")
    delivery_end = web_source.index("\ndef _is_context_only_agent_message", delivery_start)
    delivery_body = web_source[delivery_start:delivery_end]
    assert_true("agent_delivery_is_internal_graph_node", "return _chat_impl(config, payload, emit=emit)" in delivery_body)
    crawler_job_start = web_source.index("def _run_crawler_job")
    crawler_job_end = web_source.index("\ndef _run_crawler_job_agent_loop", crawler_job_start)
    crawler_job_body = web_source[crawler_job_start:crawler_job_end]
    assert_true("crawler_job_enters_langgraph", "run_crawler_job_graph(" in crawler_job_body)
    assert_true("crawler_job_uses_agent_loop_node", "agent_loop=_run_crawler_job_agent_loop" in crawler_job_body)
    assert_true("fastapi_no_chat_impl", "_chat_impl(" not in fastapi_source)
    assert_true("chat_wrapper_is_bus_only", "executor.submit(_send_agent_message, config, payload, **fields)" in web_source)
    assert_true(
        "collaboration_start_uses_chat_wrapper",
        '_send_json(self, _chat(config, payload | {"agent": "mcagent_rag"}))' in web_source,
    )
    assert_true(
        "fastapi_collaboration_start_uses_chat_wrapper",
        'return _chat(cfg(), payload | {"agent": "mcagent_rag"})' in fastapi_source,
    )


def test_crawler_job_start_requires_received_agent_message() -> None:
    tmp = tempfile.TemporaryDirectory()
    try:
        config = make_temp_config(Path(tmp.name))
        try:
            web_server._start_crawler_job_from_crawler_tool(
                config,
                {
                    "agent": "crawler_agent",
                    "question": "采集公开资料",
                    "session_id": "missing-agent-message",
                    "source": "planner",
                },
                "采集公开资料",
            )
        except RuntimeError as exc:
            assert_true("requires_agent_message", "AgentMessage" in str(exc), str(exc))
        else:
            raise AssertionError("Crawler job start accepted a direct call without an AgentMessage")
    finally:
        tmp.cleanup()


def test_crawler_job_start_rejects_payload_forged_collection_intent() -> None:
    tmp = tempfile.TemporaryDirectory()
    try:
        config = make_temp_config(Path(tmp.name))
        try:
            web_server._start_crawler_job_from_crawler_tool(
                config,
                {
                    "agent": "crawler_agent",
                    "question": "采集公开资料",
                    "session_id": "forged-intent",
                    "source": "planner",
                    "intent": "collection_request",
                },
                "采集公开资料",
            )
        except RuntimeError as exc:
            assert_true("requires_real_agent_message", "AgentMessage" in str(exc), str(exc))
        else:
            raise AssertionError("Crawler job start accepted a forged payload intent without a real AgentMessage")
    finally:
        tmp.cleanup()


def test_crawler_job_start_requires_crawler_selected_delegate_tool() -> None:
    tmp = tempfile.TemporaryDirectory()
    try:
        config = make_temp_config(Path(tmp.name))
        message = make_agent_message(
            "User",
            "采集公开资料",
            "CrawlerAgent",
            intent="collection_request",
            conversation_id="missing-tool",
            metadata={},
        )
        try:
            web_server._start_crawler_job_from_crawler_tool(
                config,
                {
                    "agent": "crawler_agent",
                    "question": "采集公开资料",
                    "session_id": "missing-tool",
                    "source": "planner",
                    "agent_message": message.to_dict(),
                },
                "采集公开资料",
            )
        except RuntimeError as exc:
            assert_true("requires_delegate_tool", "delegate_crawler" in str(exc), str(exc))
        else:
            raise AssertionError("Crawler job start accepted an AgentMessage before Crawler selected delegate_crawler")
    finally:
        tmp.cleanup()


def test_non_crawler_handoff_message_is_collection_request_not_tool_selection() -> None:
    tmp = tempfile.TemporaryDirectory()
    fake = SequencedClient(
        [
            '{"tool":"delegate_crawler","reason":"MCagent selected a Crawler handoff","collection_target":"采集公开资料","delivery_target":"MCagent/RAG"}',
            '{"proceed":true,"tool":"delegate_crawler","reason":"confirmed"}',
            '{"handoff_brief":"MCagent relays a collection request to CrawlerAgent.","reason":"handoff"}',
            '{"tool":"answer","reason":"CrawlerAgent declines collection for this fake test"}',
            '{"proceed":true,"tool":"answer","reason":"confirmed"}',
            '{"missing_side_effect":false,"action":"allow","reason":"fake decline"}',
            "CrawlerAgent fake reply.",
        ]
    )
    original_selector = web_server._selected_llm_client
    captured: list[dict[str, Any]] = []

    def fake_send(config: AppConfig, payload: dict[str, Any], *, from_agent: str, content: str, to_agent: str, metadata: dict[str, Any] | None = None, **kwargs: Any):  # noqa: ARG001
        captured.append({"from_agent": from_agent, "content": content, "to_agent": to_agent, "metadata": dict(metadata or {})})
        return {
            "answer": "CrawlerAgent fake reply.",
            "agent": "crawler_agent",
            "agent_message": make_agent_message("CrawlerAgent", "CrawlerAgent fake reply.", from_agent, requires_reply=False).to_dict(),
        }

    original_send = web_server._send_agent_message
    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake, "fake")  # type: ignore[assignment]
    web_server._send_agent_message = fake_send  # type: ignore[assignment]
    try:
        web_server._chat_impl(
            make_temp_config(Path(tmp.name)),
            {
                "agent": "mcagent_rag",
                "question": "请让 CrawlerAgent 采集公开资料并交给 MCagent/RAG",
                "session_id": "handoff-metadata-boundary",
                "model": "fake-model",
            },
        )
    finally:
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        web_server._send_agent_message = original_send  # type: ignore[assignment]
        tmp.cleanup()

    assert_true("message_sent", bool(captured))
    assert_equal("handoff_to", captured[0]["to_agent"], "CrawlerAgent")
    assert_equal("metadata_tool_is_request", captured[0]["metadata"].get("tool"), "collection_request")


def test_crawler_running_job_reuse_requires_matching_task_goal() -> None:
    tmp = tempfile.TemporaryDirectory()
    original_jobs = dict(web_server.JOBS)
    original_order = list(web_server.JOBS_ORDER)
    original_runner = web_server._run_crawler_job
    try:
        config = make_temp_config(Path(tmp.name))

        def fast_runner(job: web_server.Job, payload: dict[str, Any], config: AppConfig) -> None:  # noqa: ARG001
            web_server._update_job(job, status="running", started_at=job.started_at or 1.0, summary="stub running")

        def payload_for(text: str, *, session_id: str) -> dict[str, Any]:
            message = make_agent_message(
                "User",
                text,
                "CrawlerAgent",
                intent="collection_request",
                conversation_id=session_id,
                metadata={"tool": "delegate_crawler"},
            )
            return {
                "agent": "crawler_agent",
                "question": text,
                "session_id": session_id,
                "source": "planner",
                "delivery_target": "MCagent/RAG",
                "agent_message": message.to_dict(),
            }

        web_server._run_crawler_job = fast_runner  # type: ignore[assignment]
        with web_server.JOBS_LOCK:
            web_server.JOBS.clear()
            web_server.JOBS_ORDER.clear()

        first_job, first_created = web_server._start_crawler_job_from_crawler_tool(
            config,
            payload_for("收集 Utopia Journey 乌托邦探险之旅 Boss 名称 召唤方式 掉落清单", session_id="reuse-goal-1"),
            "收集 Utopia Journey 乌托邦探险之旅 Boss 名称 召唤方式 掉落清单",
        )
        same_job, same_created = web_server._start_crawler_job_from_crawler_tool(
            config,
            payload_for("补齐 Utopia Journey 乌托邦探险之旅 Boss 掉落和召唤方式资料", session_id="reuse-goal-2"),
            "补齐 Utopia Journey 乌托邦探险之旅 Boss 掉落和召唤方式资料",
        )
        different_job, different_created = web_server._start_crawler_job_from_crawler_tool(
            config,
            payload_for("采集 FastAPI SSE EventSource 断线重连 StreamingResponse 官方资料", session_id="reuse-goal-3"),
            "采集 FastAPI SSE EventSource 断线重连 StreamingResponse 官方资料",
        )
    finally:
        web_server._run_crawler_job = original_runner  # type: ignore[assignment]
        with web_server.JOBS_LOCK:
            web_server.JOBS.clear()
            web_server.JOBS.update(original_jobs)
            web_server.JOBS_ORDER[:] = original_order
        tmp.cleanup()

    assert_true("first_created", first_created)
    assert_true("same_reused", not same_created)
    assert_equal("same_job_id", same_job.id, first_job.id)
    assert_true("different_created", different_created)
    assert_true("different_job_id", different_job.id != first_job.id)


def test_crawler_selected_delegate_marks_existing_message_before_job_start() -> None:
    tmp = tempfile.TemporaryDirectory()
    fake = SequencedClient(
        [
            '{"tool":"delegate_crawler","reason":"collect requested data","collection_target":"采集公开资料","delivery_target":"human"}',
            '{"proceed":true,"tool":"delegate_crawler","reason":"ok"}',
            '{"handoff_brief":"CrawlerAgent accepted the user collection request.","reason":"handoff"}',
        ]
    )
    original_selector = web_server._selected_llm_client
    original_start = web_server._start_crawler_job_from_crawler_tool
    calls: list[dict[str, Any]] = []

    def fake_start(config: AppConfig, payload: dict[str, Any], question: str, plan: dict[str, Any] | None = None):  # noqa: ARG001
        message = message_from_payload(payload, default_to_agent="CrawlerAgent", default_content=question)
        calls.append({"message": message, "payload": payload, "question": question})
        job = web_server.Job(id="crawler-selected-delegate", kind="crawler", title=question, status="queued", summary="queued")
        job.result = {"plan": {"topic": question, "delivery_target": payload.get("delivery_target")}}
        return job, True

    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake, "fake")  # type: ignore[assignment]
    web_server._start_crawler_job_from_crawler_tool = fake_start  # type: ignore[assignment]
    try:
        result = web_server._send_agent_message(
            make_temp_config(Path(tmp.name)),
            {"session_id": "crawler-selected-delegate", "model": "fake-model"},
            from_agent="User",
            content="采集公开资料",
            to_agent="CrawlerAgent",
            conversation_id="crawler-selected-delegate",
        )
    finally:
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        web_server._start_crawler_job_from_crawler_tool = original_start  # type: ignore[assignment]
        tmp.cleanup()

    assert_true("job_started", bool(calls))
    message = calls[0]["message"]
    assert_equal("message_tuple", message.to_tuple(), ("User", "采集公开资料", "CrawlerAgent"))
    assert_equal("message_tool", message.metadata.get("tool"), "delegate_crawler")
    assert_equal("message_intent", message.intent, "collection_request")
    assert_true("response_job", bool(result.get("job", {}).get("id")), str(result))


def main() -> int:
    test_agent_message_tuple_and_payload_normalization()
    test_chat_records_user_to_agent_message()
    test_send_agent_message_dispatches_to_target_agent()
    test_message_bus_api_is_single_from_content_to_primitive()
    test_production_entries_do_not_bypass_message_bus_runtime()
    test_crawler_job_start_requires_received_agent_message()
    test_crawler_job_start_rejects_payload_forged_collection_intent()
    test_crawler_job_start_requires_crawler_selected_delegate_tool()
    test_non_crawler_handoff_message_is_collection_request_not_tool_selection()
    test_crawler_running_job_reuse_requires_matching_task_goal()
    test_crawler_selected_delegate_marks_existing_message_before_job_start()
    print("agent_message_bus_scenarios passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
