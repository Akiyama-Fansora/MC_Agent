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
    graph_runtime = body.get("graph_runtime") or {}
    assert_true("agent_message_graph_runtime", graph_runtime.get("runtime") == "langgraph", str(graph_runtime))
    assert_true("agent_message_graph_target", graph_runtime.get("active_agent") == "crawler_agent", str(graph_runtime))
    assert_true("agent_message_graph_node", "crawler_graph.legacy_adapter" in graph_runtime.get("visited_nodes", []), str(graph_runtime))
    agent_runtime = body.get("agent_graph_runtime") or {}
    assert_true("agent_message_agent_graph", agent_runtime.get("agent_graph") == "CrawlerAgentGraph", str(agent_runtime))
    assert_true("agent_message_source_planning_node", "crawler.prepare_source_planning_contract" in agent_runtime.get("visited_nodes", []), str(agent_runtime))
    source_planning = agent_runtime.get("source_planning_contract") or {}
    assert_true("agent_message_source_planning", source_planning.get("contract_kind") == "crawler_source_planning_input_contract", str(source_planning))
    assert_true("agent_message_source_planning_tools", "fetch_url" in source_planning.get("candidate_general_tools", []), str(source_planning))
    assert_true("agent_message_source_planning_no_plan", not {"tool", "route_intent", "sources", "tasks", "action_plan", "selected_sources"} & set(source_planning), str(source_planning))
    assert_true("agent_message_preflight_node", "crawler.prepare_message_preflight_contract" in agent_runtime.get("visited_nodes", []), str(agent_runtime))
    message_preflight = agent_runtime.get("message_preflight_contract") or {}
    assert_true("agent_message_preflight", message_preflight.get("agent_id") == "crawler_agent", str(message_preflight))
    assert_true("agent_message_preflight_no_tool", "tool" not in message_preflight and "route_intent" not in message_preflight, str(message_preflight))
    assert_true("agent_message_side_effect_auth_node", "crawler.prepare_side_effect_authorization_contract" in agent_runtime.get("visited_nodes", []), str(agent_runtime))
    side_effect_auth = agent_runtime.get("side_effect_authorization_contract") or {}
    side_effect_facts = side_effect_auth.get("facts") or {}
    assert_true("agent_message_side_effect_auth", side_effect_auth.get("contract_kind") == "crawler_side_effect_authorization_facts_contract", str(side_effect_auth))
    assert_true("agent_message_side_effect_auth_owner", side_effect_auth.get("decision_owner") == "CrawlerAgent LLM", str(side_effect_auth))
    assert_true("agent_message_side_effect_auth_surface", side_effect_auth.get("side_effect_surface") == "start_background_job", str(side_effect_auth))
    assert_true("agent_message_side_effect_auth_preflight", side_effect_auth.get("message_preflight_contract_id") == message_preflight.get("contract_id"), str(side_effect_auth))
    assert_true("agent_message_side_effect_auth_message", side_effect_facts.get("has_agent_message") is True, str(side_effect_auth))
    assert_true("agent_message_side_effect_auth_message_only", side_effect_facts.get("message_only_cannot_execute_side_effect") is True, str(side_effect_auth))
    assert_true("agent_message_side_effect_auth_no_evaluation", side_effect_auth.get("authorization_evaluation_executed") is False, str(side_effect_auth))
    assert_true("agent_message_side_effect_auth_no_execution", side_effect_auth.get("side_effect_executed") is False, str(side_effect_auth))
    assert_true("agent_message_side_effect_auth_no_tool", not {"tool", "route_intent", "action_plan", "allow", "deny", "proceed"} & set(side_effect_auth), str(side_effect_auth))
    assert_true("agent_message_route_input_node", "crawler.prepare_route_input_contract" in agent_runtime.get("visited_nodes", []), str(agent_runtime))
    route_input = agent_runtime.get("route_input_contract") or {}
    assert_true("agent_message_route_input", route_input.get("contract_kind") == "crawler_route_input_contract", str(route_input))
    assert_true("agent_message_route_input_preflight", route_input.get("message_preflight_contract_id") == message_preflight.get("contract_id"), str(route_input))
    assert_true("agent_message_route_input_source_planning", route_input.get("source_planning_contract_id") == source_planning.get("contract_id"), str(route_input))
    assert_true("agent_message_route_input_side_effect_auth", route_input.get("side_effect_authorization_contract_id") == side_effect_auth.get("contract_id"), str(route_input))
    assert_true("agent_message_route_input_no_tool", "tool" not in route_input and "route_intent" not in route_input, str(route_input))
    assert_true("agent_message_runtime_request_node", "crawler.prepare_runtime_request" in agent_runtime.get("visited_nodes", []), str(agent_runtime))
    runtime_request = agent_runtime.get("runtime_request") or {}
    assert_true("agent_message_runtime_request", runtime_request.get("contract_kind") == "crawler_collection_runtime_request", str(runtime_request))
    assert_true("agent_message_runtime_request_preflight", runtime_request.get("message_preflight_contract_id") == message_preflight.get("contract_id"), str(runtime_request))
    assert_true("agent_message_runtime_request_source_planning", runtime_request.get("source_planning_contract_id") == source_planning.get("contract_id"), str(runtime_request))
    assert_true("agent_message_runtime_request_side_effect_auth", runtime_request.get("side_effect_authorization_contract_id") == side_effect_auth.get("contract_id"), str(runtime_request))
    assert_true("agent_message_runtime_request_route_input", runtime_request.get("route_input_contract_id") == route_input.get("contract_id"), str(runtime_request))
    adapter = agent_runtime.get("runtime_adapter") or {}
    assert_true("agent_message_legacy_adapter", adapter.get("adapter") == "legacy_web_server_runtime", str(adapter))
    assert_true("agent_message_adapter_consumed_request", adapter.get("runtime_request_id") == runtime_request.get("request_id"), str(adapter))
    assert_true("agent_message_adapter_consumed_preflight", adapter.get("message_preflight_contract_id") == message_preflight.get("contract_id"), str(adapter))
    assert_true("agent_message_adapter_consumed_source_planning", adapter.get("source_planning_contract_id") == source_planning.get("contract_id"), str(adapter))
    assert_true("agent_message_adapter_consumed_side_effect_auth", adapter.get("side_effect_authorization_contract_id") == side_effect_auth.get("contract_id"), str(adapter))
    assert_true("agent_message_adapter_consumed_route_input", adapter.get("route_input_contract_id") == route_input.get("contract_id"), str(adapter))
    assert_true("agent_message_route_decision_output_node", "crawler.prepare_route_decision_output_contract" in agent_runtime.get("visited_nodes", []), str(agent_runtime))
    route_decision_output = agent_runtime.get("route_decision_output_contract") or {}
    route_decision_facts = route_decision_output.get("trace_facts") or {}
    assert_true("agent_message_route_decision_output", route_decision_output.get("contract_kind") == "crawler_route_decision_output_facts_contract", str(route_decision_output))
    assert_true("agent_message_route_decision_output_owner", route_decision_output.get("decision_owner") == "CrawlerAgent LLM", str(route_decision_output))
    assert_true("agent_message_route_decision_output_request", route_decision_output.get("runtime_request_id") == runtime_request.get("request_id"), str(route_decision_output))
    assert_true("agent_message_route_decision_output_route_input", route_decision_output.get("route_input_contract_id") == route_input.get("contract_id"), str(route_decision_output))
    assert_true("agent_message_route_decision_output_preflight", route_decision_output.get("message_preflight_contract_id") == message_preflight.get("contract_id"), str(route_decision_output))
    assert_true("agent_message_route_decision_output_source_planning", route_decision_output.get("source_planning_contract_id") == source_planning.get("contract_id"), str(route_decision_output))
    assert_true("agent_message_route_decision_output_side_effect_auth", route_decision_output.get("side_effect_authorization_contract_id") == side_effect_auth.get("contract_id"), str(route_decision_output))
    assert_true("agent_message_route_decision_output_observed_tool", route_decision_facts.get("observed_selected_tool") == "direct_answer", str(route_decision_output))
    assert_true("agent_message_route_decision_output_tool_trace", route_decision_facts.get("has_tool_selected_trace") is True, str(route_decision_output))
    assert_true("agent_message_route_decision_output_confirmation_trace", route_decision_facts.get("has_next_step_confirmation_trace") is True, str(route_decision_output))
    assert_true("agent_message_route_decision_output_confirmation_tool", route_decision_facts.get("observed_confirmation_tool") == "direct_answer", str(route_decision_output))
    assert_true("agent_message_route_decision_output_graph_did_not_decide", route_decision_output.get("route_decision_executed_by_graph") is False, str(route_decision_output))
    assert_true("agent_message_route_decision_output_no_tool", not {"tool", "route_intent", "action_plan", "proceed", "allow", "deny"} & set(route_decision_output), str(route_decision_output))
    assert_true("agent_message_route_execution_node", "crawler.prepare_route_execution_contract" in agent_runtime.get("visited_nodes", []), str(agent_runtime))
    route_execution = agent_runtime.get("route_execution_contract") or {}
    execution_trace_facts = route_execution.get("trace_facts") or {}
    execution_result_facts = route_execution.get("result_facts") or {}
    assert_true("agent_message_route_execution", route_execution.get("contract_kind") == "crawler_route_execution_facts_contract", str(route_execution))
    assert_true("agent_message_route_execution_owner", route_execution.get("decision_owner") == "CrawlerAgent LLM", str(route_execution))
    assert_true("agent_message_route_execution_request", route_execution.get("runtime_request_id") == runtime_request.get("request_id"), str(route_execution))
    assert_true("agent_message_route_execution_route_input", route_execution.get("route_input_contract_id") == route_input.get("contract_id"), str(route_execution))
    assert_true("agent_message_route_execution_route_decision", route_execution.get("route_decision_output_contract_id") == route_decision_output.get("contract_id"), str(route_execution))
    assert_true("agent_message_route_execution_preflight", route_execution.get("message_preflight_contract_id") == message_preflight.get("contract_id"), str(route_execution))
    assert_true("agent_message_route_execution_source_planning", route_execution.get("source_planning_contract_id") == source_planning.get("contract_id"), str(route_execution))
    assert_true("agent_message_route_execution_side_effect_auth", route_execution.get("side_effect_authorization_contract_id") == side_effect_auth.get("contract_id"), str(route_execution))
    assert_true("agent_message_route_execution_answer_trace", execution_trace_facts.get("has_answer_generation_trace") is True, str(route_execution))
    assert_true("agent_message_route_execution_stages", "answer" in execution_trace_facts.get("observed_execution_stages", []), str(route_execution))
    assert_true("agent_message_route_execution_answer_result", execution_result_facts.get("answer_present") is True, str(route_execution))
    assert_true("agent_message_route_execution_no_job", execution_result_facts.get("job_present") is False and execution_result_facts.get("delegation_present") is False, str(route_execution))
    assert_true("agent_message_route_execution_graph_did_not_execute", route_execution.get("route_execution_executed_by_graph") is False, str(route_execution))
    assert_true("agent_message_route_execution_no_side_effect", route_execution.get("side_effect_executed_by_contract") is False, str(route_execution))
    assert_true("agent_message_route_execution_no_tool", not {"tool", "route_intent", "action_plan", "handler", "proceed", "allow", "deny"} & set(route_execution), str(route_execution))
    assert_true("agent_message_route_result_node", "crawler.prepare_route_result_contract" in agent_runtime.get("visited_nodes", []), str(agent_runtime))
    route_result = agent_runtime.get("route_result_contract") or {}
    result_shape = route_result.get("result_shape") or {}
    assert_true("agent_message_route_result", route_result.get("contract_kind") == "crawler_route_result_contract", str(route_result))
    assert_true("agent_message_route_result_request", route_result.get("runtime_request_id") == runtime_request.get("request_id"), str(route_result))
    assert_true("agent_message_route_result_route_input", route_result.get("route_input_contract_id") == route_input.get("contract_id"), str(route_result))
    assert_true("agent_message_route_result_preflight", route_result.get("message_preflight_contract_id") == message_preflight.get("contract_id"), str(route_result))
    assert_true("agent_message_route_result_source_planning", route_result.get("source_planning_contract_id") == source_planning.get("contract_id"), str(route_result))
    assert_true("agent_message_route_result_side_effect_auth", route_result.get("side_effect_authorization_contract_id") == side_effect_auth.get("contract_id"), str(route_result))
    assert_true("agent_message_route_result_route_decision_output", route_result.get("route_decision_output_contract_id") == route_decision_output.get("contract_id"), str(route_result))
    assert_true("agent_message_route_result_route_execution", route_result.get("route_execution_contract_id") == route_execution.get("contract_id"), str(route_result))
    assert_true("agent_message_route_result_shape", result_shape.get("answer_present") is True and result_shape.get("has_agent_message") is True, str(route_result))
    assert_true("agent_message_route_result_no_tool", "tool" not in route_result and "route_intent" not in route_result and "action_plan" not in route_result, str(route_result))


def main() -> int:
    test_fastapi_core_routes()
    test_fastapi_sse_chat_shape()
    test_fastapi_agent_message_endpoint_dispatches()
    print("FASTAPI BACKEND SCENARIOS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
