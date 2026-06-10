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
    assert_true("agent_message_graph_node", "crawler_graph.graph_direct_answer_node" in graph_runtime.get("visited_nodes", []), str(graph_runtime))
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
    assert_true("agent_message_direct_answer_adapter", adapter.get("adapter") == "graph_direct_answer_node_executor", str(adapter))
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
    assert_true("agent_message_route_decision_output_graph_routed", route_decision_output.get("route_decision_executed_by_graph") is True, str(route_decision_output))
    assert_true("agent_message_route_decision_output_legacy_route_skipped", route_decision_output.get("legacy_route_still_runs_in_adapter") is False, str(route_decision_output))
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
    assert_true("agent_message_route_execution_graph_executed", route_execution.get("route_execution_executed_by_graph") is True, str(route_execution))
    assert_true("agent_message_route_execution_no_side_effect", route_execution.get("side_effect_executed_by_contract") is False, str(route_execution))
    assert_true("agent_message_route_execution_no_tool", not {"tool", "route_intent", "action_plan", "handler", "proceed", "allow", "deny"} & set(route_execution), str(route_execution))
    assert_true("agent_message_legacy_surface_node", "crawler.prepare_legacy_handler_surface_contract" in agent_runtime.get("visited_nodes", []), str(agent_runtime))
    legacy_surface = agent_runtime.get("legacy_handler_surface_contract") or {}
    surface_names = {item.get("surface") for item in legacy_surface.get("candidate_handler_surfaces", []) if isinstance(item, dict)}
    observed_surfaces = set(legacy_surface.get("observed_surface_signals") or [])
    assert_true("agent_message_legacy_surface", legacy_surface.get("contract_kind") == "crawler_legacy_handler_surface_facts_contract", str(legacy_surface))
    assert_true("agent_message_legacy_surface_owner", legacy_surface.get("decision_owner") == "CrawlerAgent LLM", str(legacy_surface))
    assert_true("agent_message_legacy_surface_request", legacy_surface.get("runtime_request_id") == runtime_request.get("request_id"), str(legacy_surface))
    assert_true("agent_message_legacy_surface_route_decision", legacy_surface.get("route_decision_output_contract_id") == route_decision_output.get("contract_id"), str(legacy_surface))
    assert_true("agent_message_legacy_surface_route_execution", legacy_surface.get("route_execution_contract_id") == route_execution.get("contract_id"), str(legacy_surface))
    assert_true("agent_message_legacy_surface_candidates", {"direct_answer", "rag_answer_generation", "delegate_crawler"}.issubset(surface_names), str(legacy_surface))
    assert_true("agent_message_legacy_surface_observed_answer", {"direct_answer", "rag_answer_generation"} & observed_surfaces, str(legacy_surface))
    assert_true("agent_message_legacy_surface_graph_did_not_select", legacy_surface.get("handler_selection_executed_by_graph") is False, str(legacy_surface))
    assert_true("agent_message_legacy_surface_graph_executed", legacy_surface.get("handler_executed_by_contract") is True, str(legacy_surface))
    assert_true("agent_message_legacy_surface_no_side_effect", legacy_surface.get("side_effect_executed_by_contract") is False, str(legacy_surface))
    assert_true("agent_message_legacy_surface_no_selected_handler", not {"tool", "route_intent", "action_plan", "handler", "selected_handler", "proceed", "allow", "deny"} & set(legacy_surface), str(legacy_surface))
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
    assert_true("agent_message_route_result_legacy_surface", route_result.get("legacy_handler_surface_contract_id") == legacy_surface.get("contract_id"), str(route_result))
    assert_true("agent_message_route_result_shape", result_shape.get("answer_present") is True and result_shape.get("has_agent_message") is True, str(route_result))
    assert_true("agent_message_route_result_no_tool", "tool" not in route_result and "route_intent" not in route_result and "action_plan" not in route_result, str(route_result))


def test_agent_selected_status_bypasses_legacy_delivery() -> None:
    fake = SequencedClient(
        [
            '{"tool":"status","reason":"inspect runtime state","collection_target":"","delivery_target":"human"}',
        ]
    )
    original_selector = web_server._selected_llm_client
    original_delivery = web_server._deliver_agent_message

    def forbidden_delivery(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("legacy delivery should not run for graph-executed status")

    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake, "fake")  # type: ignore[assignment]
    web_server._deliver_agent_message = forbidden_delivery  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            result = web_server._send_agent_message(
                make_temp_config(Path(tmp)),
                {"session_id": "fastapi-status-graph"},
                from_agent="User",
                content="status please",
                to_agent="MCagent",
                intent="user_chat",
                conversation_id="fastapi-status-graph",
            )
    finally:
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        web_server._deliver_agent_message = original_delivery  # type: ignore[assignment]

    traces = [(step.get("stage"), step.get("status")) for step in result.get("trace") or []]
    assert_true("status_trace", ("status", "next_step_confirmed") in traces, str(traces))
    agent_runtime = result.get("agent_graph_runtime") or {}
    visited = agent_runtime.get("visited_nodes") or []
    assert_true("graph_status_node", "mcagent.graph_status_route" in visited, str(visited))
    assert_true("legacy_not_visited", "mcagent.legacy_adapter" not in visited, str(visited))
    adapter = agent_runtime.get("runtime_adapter") or {}
    assert_true("graph_status_adapter", adapter.get("adapter") == "graph_status_route_executor", str(adapter))
    route_execution = agent_runtime.get("route_execution_contract") or {}
    assert_true("graph_status_execution_fact", route_execution.get("route_execution_executed_by_graph") is True, str(route_execution))
    graph_runtime = result.get("graph_runtime") or {}
    assert_true("conversation_status_node", "mcagent_graph.graph_status_route" in graph_runtime.get("visited_nodes", []), str(graph_runtime))


def test_agent_selected_direct_answer_bypasses_legacy_delivery() -> None:
    fake = SequencedClient(
        [
            '{"tool":"direct_answer","reason":"simple greeting","collection_target":"","delivery_target":"human"}',
            '{"proceed":true,"tool":"direct_answer","reason":"confirmed direct reply"}',
            '{"missing_side_effect":false,"action":"allow","reason":"direct answer is enough"}',
            "Hello from graph direct answer.",
        ]
    )
    original_selector = web_server._selected_llm_client
    original_delivery = web_server._deliver_agent_message

    def forbidden_delivery(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("legacy delivery should not run for graph-executed direct_answer")

    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake, "fake")  # type: ignore[assignment]
    web_server._deliver_agent_message = forbidden_delivery  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            result = web_server._send_agent_message(
                make_temp_config(Path(tmp)),
                {"session_id": "fastapi-direct-answer-graph"},
                from_agent="User",
                content="hello",
                to_agent="MCagent",
                intent="user_chat",
                conversation_id="fastapi-direct-answer-graph",
            )
    finally:
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        web_server._deliver_agent_message = original_delivery  # type: ignore[assignment]

    traces = [(step.get("stage"), step.get("status")) for step in result.get("trace") or []]
    assert_true("direct_answer_trace", ("answer", "generating") in traces, str(traces))
    assert_true("direct_answer_response", "Hello from graph direct answer." in result.get("answer", ""), result.get("answer", ""))
    assert_true("direct_answer_no_job", not result.get("job") and not result.get("delegation"), str(result))
    agent_runtime = result.get("agent_graph_runtime") or {}
    visited = agent_runtime.get("visited_nodes") or []
    assert_true("graph_direct_answer_node", "mcagent.graph_direct_answer_node" in visited, str(visited))
    assert_true("legacy_not_visited", "mcagent.legacy_adapter" not in visited, str(visited))
    adapter = agent_runtime.get("runtime_adapter") or {}
    assert_true("graph_direct_answer_adapter", adapter.get("adapter") == "graph_direct_answer_node_executor", str(adapter))
    route_execution = agent_runtime.get("route_execution_contract") or {}
    assert_true("graph_direct_answer_execution_fact", route_execution.get("route_execution_executed_by_graph") is True, str(route_execution))
    assert_true("graph_direct_answer_generation_fact", (route_execution.get("trace_facts") or {}).get("has_answer_generation_trace") is True, str(route_execution))
    legacy_surface = agent_runtime.get("legacy_handler_surface_contract") or {}
    assert_true("graph_direct_answer_surface_fact", legacy_surface.get("handler_executed_by_contract") is True, str(legacy_surface))
    assert_true("graph_direct_answer_surface_not_legacy", legacy_surface.get("legacy_handlers_still_run_in_adapter") is False, str(legacy_surface))
    graph_runtime = result.get("graph_runtime") or {}
    assert_true("conversation_direct_answer_node", "mcagent_graph.graph_direct_answer_node" in graph_runtime.get("visited_nodes", []), str(graph_runtime))


def test_agent_selected_crawler_audit_bypasses_legacy_delivery() -> None:
    fake = SequencedClient(
        [
            '{"tool":"crawler_audit","reason":"read recent Crawler audit","collection_target":"","delivery_target":"human"}',
            '{"proceed":true,"tool":"crawler_audit","reason":"confirmed audit read"}',
        ]
    )
    original_selector = web_server._selected_llm_client
    original_delivery = web_server._deliver_agent_message
    original_audit = web_server._recent_crawler_audit_answer

    def forbidden_delivery(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("legacy delivery should not run for graph-executed crawler_audit")

    def fake_recent_audit(_question: str) -> dict[str, object]:
        return {
            "answer": "Crawler audit: rejected 1 source.",
            "sources": [],
            "context": "",
            "agent": "mcagent_rag",
            "job": {"id": "job-audit"},
        }

    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake, "fake")  # type: ignore[assignment]
    web_server._deliver_agent_message = forbidden_delivery  # type: ignore[assignment]
    web_server._recent_crawler_audit_answer = fake_recent_audit  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            result = web_server._send_agent_message(
                make_temp_config(Path(tmp)),
                {"session_id": "fastapi-crawler-audit-graph"},
                from_agent="User",
                content="read the recent crawler audit",
                to_agent="MCagent",
                intent="user_chat",
                conversation_id="fastapi-crawler-audit-graph",
            )
    finally:
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        web_server._deliver_agent_message = original_delivery  # type: ignore[assignment]
        web_server._recent_crawler_audit_answer = original_audit  # type: ignore[assignment]

    traces = [(step.get("stage"), step.get("status")) for step in result.get("trace") or []]
    assert_true("audit_trace", ("audit", "next_step_confirmed") in traces, str(traces))
    assert_true("audit_answer_trace", ("answer", "recent_crawler_audit") in traces, str(traces))
    assert_true("audit_answer", "Crawler audit: rejected 1 source." in result.get("answer", ""), result.get("answer", ""))
    agent_runtime = result.get("agent_graph_runtime") or {}
    visited = agent_runtime.get("visited_nodes") or []
    assert_true("graph_audit_node", "mcagent.graph_crawler_audit_route" in visited, str(visited))
    assert_true("legacy_not_visited", "mcagent.legacy_adapter" not in visited, str(visited))
    adapter = agent_runtime.get("runtime_adapter") or {}
    assert_true("graph_audit_adapter", adapter.get("adapter") == "graph_crawler_audit_route_executor", str(adapter))
    route_execution = agent_runtime.get("route_execution_contract") or {}
    assert_true("graph_audit_execution_fact", route_execution.get("route_execution_executed_by_graph") is True, str(route_execution))
    legacy_surface = agent_runtime.get("legacy_handler_surface_contract") or {}
    assert_true("graph_audit_surface_fact", legacy_surface.get("handler_executed_by_contract") is True, str(legacy_surface))
    assert_true("graph_audit_surface_not_legacy", legacy_surface.get("legacy_handlers_still_run_in_adapter") is False, str(legacy_surface))
    graph_runtime = result.get("graph_runtime") or {}
    assert_true("conversation_audit_node", "mcagent_graph.graph_crawler_audit_route" in graph_runtime.get("visited_nodes", []), str(graph_runtime))


def test_agent_selected_safe_local_inventory_bypasses_legacy_delivery() -> None:
    fake = SequencedClient(
        [
            '{"tool":"local_corpus_inventory","reason":"inspect local corpus coverage","collection_target":"","delivery_target":"human"}',
            '{"proceed":true,"tool":"local_corpus_inventory","reason":"confirmed inventory read"}',
            '{"missing_side_effect":false,"action":"allow","reason":"inventory read is sufficient"}',
        ]
    )
    original_selector = web_server._selected_llm_client
    original_delivery = web_server._deliver_agent_message
    original_inventory = web_server._local_corpus_inventory_answer

    def forbidden_delivery(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("legacy delivery should not run for graph-executed local_corpus_inventory")

    def fake_inventory(_config: AppConfig, _question: str) -> dict[str, object]:
        return {
            "answer": "Local inventory: 2 indexed documents.",
            "sources": [{"title": "local inventory", "document_id": 1}],
            "context": "inventory context",
            "agent": "mcagent_rag",
        }

    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake, "fake")  # type: ignore[assignment]
    web_server._deliver_agent_message = forbidden_delivery  # type: ignore[assignment]
    web_server._local_corpus_inventory_answer = fake_inventory  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            result = web_server._send_agent_message(
                make_temp_config(Path(tmp)),
                {"session_id": "fastapi-inventory-graph"},
                from_agent="User",
                content="inspect local corpus coverage",
                to_agent="MCagent",
                intent="user_chat",
                conversation_id="fastapi-inventory-graph",
            )
    finally:
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        web_server._deliver_agent_message = original_delivery  # type: ignore[assignment]
        web_server._local_corpus_inventory_answer = original_inventory  # type: ignore[assignment]

    traces = [(step.get("stage"), step.get("status")) for step in result.get("trace") or []]
    assert_true("inventory_confirmed", ("retrieve", "inventory_next_step_confirmed") in traces, str(traces))
    assert_true("inventory_scanning", ("retrieve", "inventory_scanning") in traces, str(traces))
    assert_true("inventory_done", ("retrieve", "inventory_done") in traces, str(traces))
    assert_true("inventory_answer", "Local inventory: 2 indexed documents." in result.get("answer", ""), result.get("answer", ""))
    assert_true("inventory_no_job", not result.get("job") and not result.get("delegation"), str(result))
    agent_runtime = result.get("agent_graph_runtime") or {}
    visited = agent_runtime.get("visited_nodes") or []
    assert_true("graph_inventory_node", "mcagent.graph_local_corpus_inventory_route" in visited, str(visited))
    assert_true("legacy_not_visited", "mcagent.legacy_adapter" not in visited, str(visited))
    adapter = agent_runtime.get("runtime_adapter") or {}
    assert_true("graph_inventory_adapter", adapter.get("adapter") == "graph_local_corpus_inventory_route_executor", str(adapter))
    route_execution = agent_runtime.get("route_execution_contract") or {}
    assert_true("graph_inventory_execution_fact", route_execution.get("route_execution_executed_by_graph") is True, str(route_execution))
    legacy_surface = agent_runtime.get("legacy_handler_surface_contract") or {}
    assert_true("graph_inventory_surface_fact", legacy_surface.get("handler_executed_by_contract") is True, str(legacy_surface))
    assert_true("graph_inventory_surface_not_legacy", legacy_surface.get("legacy_handlers_still_run_in_adapter") is False, str(legacy_surface))
    graph_runtime = result.get("graph_runtime") or {}
    assert_true("conversation_inventory_node", "mcagent_graph.graph_local_corpus_inventory_route" in graph_runtime.get("visited_nodes", []), str(graph_runtime))


def test_agent_selected_router_error_bypasses_legacy_delivery() -> None:
    fake = SequencedClient(
        [
            '{"tool":"not_a_real_tool","reason":"invalid route selected by router","collection_target":"","delivery_target":""}',
        ]
    )
    original_selector = web_server._selected_llm_client
    original_delivery = web_server._deliver_agent_message

    def forbidden_delivery(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("legacy delivery should not run for graph-executed router_error")

    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake, "fake")  # type: ignore[assignment]
    web_server._deliver_agent_message = forbidden_delivery  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            result = web_server._send_agent_message(
                make_temp_config(Path(tmp)),
                {"session_id": "fastapi-router-error-graph"},
                from_agent="User",
                content="trigger invalid tool selection",
                to_agent="MCagent",
                intent="user_chat",
                conversation_id="fastapi-router-error-graph",
            )
    finally:
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        web_server._deliver_agent_message = original_delivery  # type: ignore[assignment]

    traces = [(step.get("stage"), step.get("status")) for step in result.get("trace") or []]
    assert_true("router_error_trace", ("done", "router_error") in traces, str(traces))
    assert_true("router_error_no_sources", result.get("sources") == [] and not result.get("context"), str(result))
    assert_true("router_error_no_job", not result.get("job") and not result.get("delegation"), str(result))
    agent_runtime = result.get("agent_graph_runtime") or {}
    visited = agent_runtime.get("visited_nodes") or []
    assert_true("graph_router_error_node", "mcagent.graph_router_error_route" in visited, str(visited))
    assert_true("legacy_not_visited", "mcagent.legacy_adapter" not in visited, str(visited))
    adapter = agent_runtime.get("runtime_adapter") or {}
    assert_true("graph_router_error_adapter", adapter.get("adapter") == "graph_router_error_route_executor", str(adapter))
    route_execution = agent_runtime.get("route_execution_contract") or {}
    assert_true("graph_router_error_execution_fact", route_execution.get("route_execution_executed_by_graph") is True, str(route_execution))
    assert_true("graph_router_error_trace_fact", (route_execution.get("trace_facts") or {}).get("has_router_error_trace") is True, str(route_execution))
    legacy_surface = agent_runtime.get("legacy_handler_surface_contract") or {}
    assert_true("graph_router_error_surface_fact", legacy_surface.get("handler_executed_by_contract") is True, str(legacy_surface))
    assert_true("graph_router_error_surface_not_legacy", legacy_surface.get("legacy_handlers_still_run_in_adapter") is False, str(legacy_surface))
    graph_runtime = result.get("graph_runtime") or {}
    assert_true("conversation_router_error_node", "mcagent_graph.graph_router_error_route" in graph_runtime.get("visited_nodes", []), str(graph_runtime))


def test_local_inventory_with_delegate_plan_stays_on_legacy_delivery() -> None:
    fake = SequencedClient(
        [
            '{"tool":"local_corpus_inventory","reason":"inspect then delegate","action_plan":[{"step":1,"tool":"local_corpus_inventory","goal":"inspect local coverage"},{"step":2,"tool":"delegate_crawler","goal":"collect missing evidence"}],"collection_target":"collect missing evidence","delivery_target":"MCagent/RAG"}',
            '{"proceed":true,"tool":"local_corpus_inventory","reason":"confirmed inventory read"}',
        ]
    )
    original_selector = web_server._selected_llm_client
    original_delivery = web_server._deliver_agent_message
    calls: list[dict[str, object]] = []

    def fake_delivery(_config: AppConfig, payload: dict[str, object], emit=None) -> dict[str, object]:  # noqa: ANN001, ARG001
        decision = payload.get("_graph_route_decision") if isinstance(payload.get("_graph_route_decision"), dict) else {}
        calls.append({"route_intent": decision.get("route_intent"), "action_plan": decision.get("action_plan")})
        return {"answer": "legacy inventory path used", "sources": [], "context": "", "agent": "mcagent_rag"}

    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake, "fake")  # type: ignore[assignment]
    web_server._deliver_agent_message = fake_delivery  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            result = web_server._send_agent_message(
                make_temp_config(Path(tmp)),
                {"session_id": "fastapi-inventory-legacy"},
                from_agent="User",
                content="inspect local corpus then collect missing evidence",
                to_agent="MCagent",
                intent="user_chat",
                conversation_id="fastapi-inventory-legacy",
            )
    finally:
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        web_server._deliver_agent_message = original_delivery  # type: ignore[assignment]

    assert_true("legacy_delivery_called", len(calls) == 1 and calls[0].get("route_intent") == "local_corpus_inventory", str(calls))
    agent_runtime = result.get("agent_graph_runtime") or {}
    visited = agent_runtime.get("visited_nodes") or []
    assert_true("legacy_inventory_node", "mcagent.legacy_adapter" in visited, str(visited))
    assert_true("graph_inventory_not_visited", "mcagent.graph_local_corpus_inventory_route" not in visited, str(visited))
    adapter = agent_runtime.get("runtime_adapter") or {}
    assert_true("legacy_inventory_adapter", adapter.get("adapter") == "legacy_web_server_runtime", str(adapter))
    graph_runtime = result.get("graph_runtime") or {}
    assert_true("conversation_legacy_node", "mcagent_graph.legacy_adapter" in graph_runtime.get("visited_nodes", []), str(graph_runtime))


def main() -> int:
    test_fastapi_core_routes()
    test_fastapi_sse_chat_shape()
    test_fastapi_agent_message_endpoint_dispatches()
    test_agent_selected_status_bypasses_legacy_delivery()
    test_agent_selected_direct_answer_bypasses_legacy_delivery()
    test_agent_selected_crawler_audit_bypasses_legacy_delivery()
    test_agent_selected_safe_local_inventory_bypasses_legacy_delivery()
    test_agent_selected_router_error_bypasses_legacy_delivery()
    test_local_inventory_with_delegate_plan_stays_on_legacy_delivery()
    print("FASTAPI BACKEND SCENARIOS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
