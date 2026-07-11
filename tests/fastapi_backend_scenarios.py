from __future__ import annotations

from pathlib import Path
import json
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
from mcagent.rag_service import RagRetrievalResult  # noqa: E402
from mcagent.schema import SearchResult  # noqa: E402
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
        self.calls: list[dict[str, object]] = []

    def chat(self, messages, *, temperature=None, max_tokens=None):  # noqa: ANN001, ANN201, ARG002
        self.calls.append({"messages": messages, "temperature": temperature, "max_tokens": max_tokens})
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


def test_fastapi_preview_limits_tolerate_malformed_payload_values() -> None:
    calls: list[int] = []
    original_retriever = web_server.Retriever

    class FakeRetriever:
        def __init__(self, _config: AppConfig) -> None:
            pass

        def search(self, _query: str, *, top_k: int, session_summary: dict[str, object] | None = None):  # noqa: ANN201, ARG002
            calls.append(top_k)
            return []

    web_server.Retriever = FakeRetriever  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            client = TestClient(create_app(make_temp_config(Path(tmp))))

            malformed_search = client.post("/api/search", json={"query": "diamond tools", "top_k": "many"})
            assert_true("malformed_search_ok", malformed_search.status_code == 200, malformed_search.text)
            assert_true("malformed_search_clamped", isinstance(calls[-1], int) and 1 <= calls[-1] <= web_server.MAX_ROUGH_TOP_K, str(calls))

            low_search = client.post("/api/search", json={"query": "diamond tools", "top_k": "-7"})
            assert_true("low_search_ok", low_search.status_code == 200, low_search.text)
            assert_true("low_search_clamped", calls[-1] == 1, str(calls))

            high_search = client.post("/api/search", json={"query": "diamond tools", "top_k": "9999"})
            assert_true("high_search_ok", high_search.status_code == 200, high_search.text)
            assert_true("high_search_clamped", calls[-1] == web_server.MAX_ROUGH_TOP_K, str(calls))

            malformed_summary = client.post("/api/crawler/summary", json={"limit": "many"})
            assert_true("malformed_summary_ok", malformed_summary.status_code == 200, malformed_summary.text)
            assert_true("malformed_summary_default", malformed_summary.json().get("limit") == 20, str(malformed_summary.json()))

            low_summary = client.post("/api/crawler/summary", json={"limit": "-5"})
            assert_true("low_summary_ok", low_summary.status_code == 200, low_summary.text)
            assert_true("low_summary_clamped", low_summary.json().get("limit") == 1, str(low_summary.json()))
    finally:
        web_server.Retriever = original_retriever  # type: ignore[assignment]


def test_fastapi_sse_chat_shape() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client = TestClient(create_app(make_temp_config(Path(tmp))))
        with client.stream("POST", "/api/chat/stream", json={"question": "", "session_id": "sse-test"}) as response:
            text = "".join(response.iter_text())
        assert_true("sse_status", response.status_code == 200)
        assert_true("sse_response_event", "event: response" in text)
        assert_true("sse_agent_message", '"agent_message"' in text)
        assert_true("sse_done_event", "event: done" in text)

        with client.stream("POST", "/api/chat/stream", json={"question": "status", "session_id": "sse-status-test"}) as response:
            status_text = "".join(response.iter_text())
        assert_true("sse_status_command_status", response.status_code == 200)
        assert_true("sse_status_command_response", "event: response" in status_text, status_text[:500])
        assert_true("sse_status_command_done", "event: done" in status_text, status_text[:500])


def _sse_events(text: str) -> list[tuple[str, object]]:
    events: list[tuple[str, object]] = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        event = "message"
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].lstrip())
        if not data_lines:
            continue
        raw = "\n".join(data_lines)
        try:
            events.append((event, json.loads(raw)))
        except json.JSONDecodeError:
            events.append((event, raw))
    return events


def test_fastapi_stream_local_inventory_question_uses_inventory_observation_not_rag() -> None:
    router_reply = (
        '{"tool":"local_corpus_inventory","reason":"the user asks what the local library contains, not a specific fact",'
        '"collection_target":"","delivery_target":"human"}'
    )
    final_reply = (
        "I inspected the objective local corpus observation. Besides Utopian Journey, "
        "the raw title/path/preview evidence also shows VanillaEra, Closing Song, Craftoria, and Prominence II."
    )
    fake = SequencedClient([router_reply, final_reply])
    original_selector = web_server._selected_llm_client
    original_inventory = web_server._local_corpus_inventory_answer

    def fake_inventory(_config: AppConfig, _question: str) -> dict[str, object]:
        return {
            "answer": "local_corpus_inventory observation: database currently has 5 indexed documents.",
            "sources": [{"title": "VanillaEra:FaresChron", "document_id": 1}],
            "context": "inventory context",
            "agent": "mcagent_rag",
            "metadata": {
                "inventory_observation": {
                    "document_count": 5,
                    "scanned_documents": 5,
                    "tool_boundary": "raw evidence visible; candidates are not final judgments",
                    "entity_candidates": [
                        {
                            "name": "VanillaEra:FaresChron",
                            "aliases": ["VanillaEra"],
                            "related_documents": 1,
                            "buckets": {"modpack": 1},
                            "mechanical_reasons": ["manifest_fact"],
                            "raw_evidence": [
                                {
                                    "raw_title": "VanillaEra:FaresChron manifest structured fact",
                                    "raw_source_path": "D:/case/vefc/modpack_manifests.json",
                                    "raw_preview": "modpack name VanillaEra:FaresChron",
                                    "reason": "manifest_fact",
                                }
                            ],
                        },
                        {
                            "name": "Closing Song",
                            "aliases": ["Closing Song"],
                            "related_documents": 1,
                            "buckets": {"pack_internal": 1},
                            "mechanical_reasons": ["pack_internal_path"],
                            "raw_evidence": [
                                {
                                    "raw_title": "closing song pack internals",
                                    "raw_source_path": "D:/case/closing_song/pack_internal/quests.md",
                                    "raw_preview": "Closing Song 1.5.1 pack internal inventory",
                                    "reason": "pack_internal_path",
                                }
                            ],
                        },
                    ],
                    "bucket_summary": [{"bucket": "modpack", "documents": 2, "distinct_titles": 2}],
                }
            },
        }

    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake, "fake")  # type: ignore[assignment]
    web_server._local_corpus_inventory_answer = fake_inventory  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            client = TestClient(create_app(make_temp_config(Path(tmp))))
            with client.stream(
                "POST",
                "/api/chat/stream",
                json={"question": "what modpacks are in the local library", "session_id": "inventory-stream-test", "model": "fake-model"},
            ) as response:
                text = "".join(response.iter_text())
    finally:
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        web_server._local_corpus_inventory_answer = original_inventory  # type: ignore[assignment]

    assert_true("inventory_stream_status", response.status_code == 200)
    assert_true("inventory_answer", "VanillaEra" in text and "Closing Song" in text, text[:1000])
    assert_true("inventory_not_rag", "graph_rag_answer_route" not in text, text[:1000])

def test_fastapi_agent_message_endpoint_dispatches() -> None:
    fake = SequencedClient(
        [
            '{"tool":"direct_answer","reason":"simple greeting","collection_target":"浣犲ソ","delivery_target":"human"}',
            "Hello, I am CrawlerAgent.",
        ]
    )
    original_selector = web_server._selected_llm_client
    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake, "fake")  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            client = TestClient(create_app(make_temp_config(Path(tmp))))
            response = client.post(
                "/api/agent-message",
                json={"from_agent": "User", "to_agent": "CrawlerAgent", "content": "浣犲ソ", "session_id": "fastapi-message"},
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


def test_mcagent_rag_answer_executes_in_graph_node() -> None:
    fake = SequencedClient(
        [
            '{"tool":"answer","reason":"answer from local evidence","collection_target":"","delivery_target":"human"}',
            "Craftoria is available in the local evidence. [S1]",
            '{"violation":false,"reason":"answer did not claim unexecuted crawler work"}',
        ]
    )
    original_selector = web_server._selected_llm_client
    original_delivery = web_server._deliver_agent_message
    original_retrieve = web_server.RagRetrievalService.retrieve

    def forbidden_delivery(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("legacy delivery should not run for graph-executed RAG answer")

    def fake_retrieve(self, config, *, agent, original_question, question, session_summary, preparation, use_planner, add_trace):  # noqa: ANN001, ANN202, ARG001
        result_1 = SearchResult(
            rank=1,
            score=0.98,
            chunk_id=1,
            document_id=10,
            chunk_index=0,
            title="Craftoria local guide",
            source_path="D:/case/data/crawler_exports/modrinth_agent/craftoria.md",
            url="https://example.test/craftoria",
            text="Craftoria beginner guide evidence from local corpus. This page explains beginner progression and quests.",
            metadata={"source": "modrinth_api", "project": "Craftoria"},
        )
        result_2 = SearchResult(
            rank=2,
            score=0.94,
            chunk_id=2,
            document_id=11,
            chunk_index=0,
            title="Craftoria wiki progression",
            source_path="D:/case/data/crawler_exports/web_discovery/craftoria_wiki.md",
            url="https://example.test/craftoria/wiki",
            text="Craftoria wiki evidence with beginner route, early resources, and quest progression.",
            metadata={"source": "wiki", "project": "Craftoria"},
        )
        add_trace("retrieve", "done", {"results": 2, "top": result_1.title})
        return RagRetrievalResult(
            evidence_question=preparation.evidence_question,
            rough_k=preparation.rough_k,
            final_k=preparation.final_k,
            retrieval_plan=None,
            search_question=preparation.evidence_question,
            rough_results=[result_1, result_2],
            selected=[result_1, result_2],
        )

    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake, "fake")  # type: ignore[assignment]
    web_server._deliver_agent_message = forbidden_delivery  # type: ignore[assignment]
    web_server.RagRetrievalService.retrieve = fake_retrieve  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            result = web_server._send_agent_message(
                make_temp_config(Path(tmp)),
                {"session_id": "fastapi-rag-answer-graph"},
                from_agent="User",
                content="What does local evidence say about Craftoria?",
                to_agent="MCagent",
                intent="user_chat",
                conversation_id="fastapi-rag-answer-graph",
            )
    finally:
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        web_server._deliver_agent_message = original_delivery  # type: ignore[assignment]
        web_server.RagRetrievalService.retrieve = original_retrieve  # type: ignore[assignment]

    answer = str(result.get("answer") or "")
    assert_true("rag_answer_response", "Craftoria is available" in answer, answer)
    assert_true("rag_answer_sources", bool(result.get("sources")), str(result))
    assert_true("rag_answer_no_job", not result.get("job") and not result.get("delegation"), str(result))
    agent_runtime = result.get("agent_graph_runtime") or {}
    visited = agent_runtime.get("visited_nodes") or []
    assert_true("graph_rag_answer_node", "mcagent.graph_rag_answer_route" in visited, str(visited))
    assert_true("rag_legacy_not_visited", "mcagent.legacy_adapter" not in visited, str(visited))
    adapter = agent_runtime.get("runtime_adapter") or {}
    assert_true("graph_rag_answer_adapter", adapter.get("adapter") == "graph_rag_answer_route_executor", str(adapter))
    route_execution = agent_runtime.get("route_execution_contract") or {}
    assert_true("graph_rag_answer_execution_fact", route_execution.get("route_execution_executed_by_graph") is True, str(route_execution))
    assert_true("graph_rag_answer_trace_fact", (route_execution.get("trace_facts") or {}).get("has_answer_generation_trace") is True, str(route_execution))
    legacy_surface = agent_runtime.get("legacy_handler_surface_contract") or {}
    assert_true("graph_rag_answer_surface_fact", legacy_surface.get("handler_executed_by_contract") is True, str(legacy_surface))
    assert_true("graph_rag_answer_surface_not_legacy", legacy_surface.get("legacy_handlers_still_run_in_adapter") is False, str(legacy_surface))
    graph_runtime = result.get("graph_runtime") or {}
    assert_true("conversation_rag_answer_node", "mcagent_graph.graph_rag_answer_route" in graph_runtime.get("visited_nodes", []), str(graph_runtime))


def test_mcagent_local_rag_search_executes_in_graph_node() -> None:
    fake = SequencedClient(
        [
            '{"tool":"local_rag_search","reason":"search local evidence","collection_target":"","delivery_target":"human"}',
            '{"proceed":false,"tool":"answer","suggested_tool":"local_rag_search","reason":"answer should be normalized to local_rag_search"}',
            "Modern Industrialization tweaks are present in local Craftoria evidence. [S1]",
            '{"violation":false,"reason":"answer did not claim unexecuted crawler work"}',
        ]
    )
    original_selector = web_server._selected_llm_client
    original_delivery = web_server._deliver_agent_message
    original_retrieve = web_server.RagRetrievalService.retrieve

    def forbidden_delivery(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("legacy delivery should not run for graph-executed local_rag_search")

    def fake_retrieve(self, config, *, agent, original_question, question, session_summary, preparation, use_planner, add_trace):  # noqa: ANN001, ANN202, ARG001
        result_1 = SearchResult(
            rank=1,
            score=0.98,
            chunk_id=1,
            document_id=20,
            chunk_index=0,
            title="Craftoria Modern Industrialization tweaks",
            source_path="D:/case/data/crawler_exports/modrinth_agent/craftoria_mi.md",
            url="https://example.test/craftoria/mi",
            text="Craftoria local evidence says Modern Industrialization tweaks include KubeJS recipe and machine changes.",
            metadata={"source": "modrinth_api", "project": "Craftoria"},
        )
        result_2 = SearchResult(
            rank=2,
            score=0.93,
            chunk_id=2,
            document_id=21,
            chunk_index=0,
            title="Craftoria KubeJS MI scripts",
            source_path="D:/case/data/crawler_exports/web_discovery/craftoria_kubejs.md",
            url="https://example.test/craftoria/kubejs",
            text="Craftoria evidence mentions KubeJS scripts changing Modern Industrialization progression.",
            metadata={"source": "web_discovery", "project": "Craftoria"},
        )
        add_trace("retrieve", "done", {"results": 2, "top": result_1.title})
        return RagRetrievalResult(
            evidence_question=preparation.evidence_question,
            rough_k=preparation.rough_k,
            final_k=preparation.final_k,
            retrieval_plan=None,
            search_question=preparation.evidence_question,
            rough_results=[result_1, result_2],
            selected=[result_1, result_2],
        )

    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake, "fake")  # type: ignore[assignment]
    web_server._deliver_agent_message = forbidden_delivery  # type: ignore[assignment]
    web_server.RagRetrievalService.retrieve = fake_retrieve  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            result = web_server._send_agent_message(
                make_temp_config(Path(tmp)),
                {"session_id": "fastapi-local-rag-search-graph"},
                from_agent="User",
                content="What does local evidence say about Craftoria Modern Industrialization tweaks?",
                to_agent="MCagent",
                intent="user_chat",
                conversation_id="fastapi-local-rag-search-graph",
            )
    finally:
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        web_server._deliver_agent_message = original_delivery  # type: ignore[assignment]
        web_server.RagRetrievalService.retrieve = original_retrieve  # type: ignore[assignment]

    answer = str(result.get("answer") or "")
    assert_true("local_rag_answer_response", "Modern Industrialization tweaks" in answer, answer)
    agent_runtime = result.get("agent_graph_runtime") or {}
    visited = agent_runtime.get("visited_nodes") or []
    assert_true("graph_local_rag_answer_node", "mcagent.graph_rag_answer_route" in visited, str(visited))
    assert_true("local_rag_legacy_not_visited", "mcagent.legacy_adapter" not in visited, str(visited))
    adapter = agent_runtime.get("runtime_adapter") or {}
    assert_true("graph_local_rag_adapter", adapter.get("adapter") == "graph_rag_answer_route_executor", str(adapter))
    route_decision = agent_runtime.get("route_decision") or {}
    confirmation = route_decision.get("route_confirmation") if isinstance(route_decision.get("route_confirmation"), dict) else {}
    assert_true("local_rag_route_intent_is_rag_family", route_decision.get("route_intent") in {"answer", "local_rag_search"}, str(route_decision))
    assert_true("local_rag_runtime_preflight", confirmation.get("tool") == "answer" and confirmation.get("planner") == "runtime_preflight", str(route_decision))
    graph_runtime = result.get("graph_runtime") or {}
    assert_true("conversation_local_rag_node", "mcagent_graph.graph_rag_answer_route" in graph_runtime.get("visited_nodes", []), str(graph_runtime))


def test_mcagent_local_rag_empty_result_executes_in_graph_node() -> None:
    fake = SequencedClient(
        [
            '{"tool":"local_rag_search","reason":"search local evidence","collection_target":"","delivery_target":"human"}',
            '{"proceed":true,"tool":"local_rag_search","reason":"confirmed local-only search"}',
        ]
    )
    original_selector = web_server._selected_llm_client
    original_delivery = web_server._deliver_agent_message
    original_retrieve = web_server.RagRetrievalService.retrieve

    def forbidden_delivery(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("legacy delivery should not run for graph-executed empty local_rag_search")

    def fake_retrieve(self, config, *, agent, original_question, question, session_summary, preparation, use_planner, add_trace):  # noqa: ANN001, ANN202, ARG001
        add_trace("retrieve", "done", {"results": 0})
        return RagRetrievalResult(
            evidence_question=preparation.evidence_question,
            rough_k=preparation.rough_k,
            final_k=preparation.final_k,
            retrieval_plan=None,
            search_question=preparation.evidence_question,
            rough_results=[],
            selected=[],
        )

    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake, "fake")  # type: ignore[assignment]
    web_server._deliver_agent_message = forbidden_delivery  # type: ignore[assignment]
    web_server.RagRetrievalService.retrieve = fake_retrieve  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            result = web_server._send_agent_message(
                make_temp_config(Path(tmp)),
                {"session_id": "fastapi-local-rag-empty-graph"},
                from_agent="User",
                content="Use local evidence only for a nonexistent Craftoria subtopic.",
                to_agent="MCagent",
                intent="user_chat",
                conversation_id="fastapi-local-rag-empty-graph",
            )
    finally:
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        web_server._deliver_agent_message = original_delivery  # type: ignore[assignment]
        web_server.RagRetrievalService.retrieve = original_retrieve  # type: ignore[assignment]

    answer = str(result.get("answer") or "")
    assert_true("empty_rag_answer_insufficient", "Crawler" in answer, answer)
    assert_true("empty_rag_no_job", not result.get("job") and not result.get("delegation"), str(result))
    agent_runtime = result.get("agent_graph_runtime") or {}
    visited = agent_runtime.get("visited_nodes") or []
    assert_true("empty_rag_graph_node", "mcagent.graph_rag_answer_route" in visited, str(visited))
    assert_true("empty_rag_legacy_not_visited", "mcagent.legacy_adapter" not in visited, str(visited))
    route_execution = agent_runtime.get("route_execution_contract") or {}
    trace_facts = route_execution.get("trace_facts") or {}
    assert_true("empty_rag_execution_graph", route_execution.get("route_execution_executed_by_graph") is True, str(route_execution))
    assert_true("empty_rag_insufficient_trace", trace_facts.get("has_insufficient_evidence_trace") is True, str(route_execution))
    legacy_surface = agent_runtime.get("legacy_handler_surface_contract") or {}
    assert_true("empty_rag_surface_fact", legacy_surface.get("handler_executed_by_contract") is True, str(legacy_surface))
    assert_true("empty_rag_surface_not_legacy", legacy_surface.get("legacy_handlers_still_run_in_adapter") is False, str(legacy_surface))
    assert_true("empty_rag_surface_observed", "no_retrieval_results" in (legacy_surface.get("observed_surface_signals") or []), str(legacy_surface))
    graph_runtime = result.get("graph_runtime") or {}
    assert_true("conversation_empty_rag_node", "mcagent_graph.graph_rag_answer_route" in graph_runtime.get("visited_nodes", []), str(graph_runtime))


def test_mcagent_context_request_executes_in_graph_node() -> None:
    original_delivery = web_server._deliver_agent_message
    original_retriever = web_server.Retriever

    def forbidden_delivery(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("legacy delivery should not run for graph-executed mcagent_context_reply")

    class FakeRetriever:
        def __init__(self, _config: AppConfig) -> None:
            pass

        def search(self, query: str, *, top_k: int, session_summary: dict[str, object] | None = None):  # noqa: ANN201, ARG002
            return [
                SearchResult(
                    rank=1,
                    score=0.97,
                    chunk_id=101,
                    document_id=501,
                    chunk_index=0,
                    title="Craftoria local context",
                    source_path="D:/case/data/crawler_exports/mcagent_context/craftoria.md",
                    url="https://example.test/craftoria",
                    text=(
                        "Craftoria local context says the local library has pack internals and version notes, "
                        "but still lacks a public beginner guide, quest route, and reliable gameplay walkthrough."
                    ),
                    metadata={"source": "mcagent_context", "project": "Craftoria"},
                )
            ]

    web_server._deliver_agent_message = forbidden_delivery  # type: ignore[assignment]
    web_server.Retriever = FakeRetriever  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            result = web_server._send_agent_message(
                make_temp_config(Path(tmp)),
                {
                    "session_id": "fastapi-mcagent-context-reply-graph",
                    "mcagent_context_request": {
                        "collection_target": "Craftoria modpack",
                        "focus": "Craftoria modpack beginner guide gaps",
                    },
                },
                from_agent="CrawlerAgent",
                content="Tell me what local Craftoria evidence and gaps exist",
                to_agent="MCagent",
                intent="mcagent_context_request",
                conversation_id="fastapi-mcagent-context-reply-graph",
                metadata={"tool": "mcagent_context", "collection_target": "Craftoria modpack"},
            )
    finally:
        web_server._deliver_agent_message = original_delivery  # type: ignore[assignment]
        web_server.Retriever = original_retriever  # type: ignore[assignment]

    answer = str(result.get("answer") or "")
    assert_true("mcagent_context_first_person", "CrawlerAgent" in answer, answer)
    assert_true("mcagent_context_mentions_evidence", "Craftoria local context" in answer or "Craftoria local gap" in answer, answer)
    assert_true("mcagent_context_no_job", not result.get("job") and not result.get("delegation"), str(result))
    traces = [(step.get("stage"), step.get("status")) for step in result.get("trace") or []]
    assert_true("mcagent_context_light_done_trace", ("retrieve", "mcagent_context_light_done") in traces, str(traces))
    agent_runtime = result.get("agent_graph_runtime") or {}
    visited = agent_runtime.get("visited_nodes") or []
    assert_true("mcagent_context_graph_node", "mcagent.graph_mcagent_context_reply" in visited, str(visited))
    assert_true("mcagent_context_legacy_not_visited", "mcagent.legacy_adapter" not in visited, str(visited))
    adapter = agent_runtime.get("runtime_adapter") or {}
    assert_true("mcagent_context_adapter", adapter.get("adapter") == "graph_mcagent_context_reply_executor", str(adapter))
    route_execution = agent_runtime.get("route_execution_contract") or {}
    assert_true("mcagent_context_execution_graph", route_execution.get("route_execution_executed_by_graph") is True, str(route_execution))
    legacy_surface = agent_runtime.get("legacy_handler_surface_contract") or {}
    assert_true("mcagent_context_surface_observed", "mcagent_context_reply" in (legacy_surface.get("observed_surface_signals") or []), str(legacy_surface))
    assert_true("mcagent_context_surface_not_legacy", legacy_surface.get("legacy_handlers_still_run_in_adapter") is False, str(legacy_surface))
    graph_runtime = result.get("graph_runtime") or {}
    assert_true("conversation_mcagent_context_node", "mcagent_graph.graph_mcagent_context_reply" in graph_runtime.get("visited_nodes", []), str(graph_runtime))


def test_mcagent_direct_answer_protocol_explains_crawler_contact_as_agent_message() -> None:
    fake = SequencedClient(
        [
            '{"tool":"direct_answer","reason":"user asks architecture/protocol question","collection_target":"","delivery_target":"human"}',
            '{"should_send":false,"reason":"protocol explanation, not a request to message CrawlerAgent"}',
            "I contact CrawlerAgent only through AgentMessage(from_agent, content, to_agent); MCagent route tools expose agent_message, and CrawlerAgent chooses its own tools after receiving the message.",
        ]
    )
    original_selector = web_server._selected_llm_client
    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake, "fake")  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            result = web_server._send_agent_message(
                make_temp_config(Path(tmp)),
                {"session_id": "fastapi-mcagent-protocol-direct"},
                from_agent="User",
                content="Is Crawler contacted through agent_message or a special delegate function?",
                to_agent="MCagent",
                intent="user_chat",
                conversation_id="fastapi-mcagent-protocol-direct",
            )
    finally:
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]

    answer = str(result.get("answer") or "")
    assert_true("protocol_answer_mentions_agent_message", "AgentMessage" in answer and "agent_message" in answer, answer)
    direct_prompt = fake.calls[0]["messages"][0]["content"] + "\n" + fake.calls[0]["messages"][1]["content"]
    assert_true("direct_prompt_only_agent_message", "AgentMessage" in direct_prompt, direct_prompt)
    assert_true("direct_prompt_uses_capability_fact_not_keyword_ban", "agent_message" in direct_prompt, direct_prompt)
    assert_true("protocol_no_job", not result.get("job") and not result.get("delegation"), str(result))


def test_mcagent_can_message_crawler_agent_without_collection_side_effect() -> None:
    fake = SequencedClient(
        [
            '{"tool":"agent_message","reason":"user wants another Agent to answer","to_agent":"CrawlerAgent","content":"1+1 equals what?","intent":"agent_question","delivery_target":"human"}',
            '{"requires_gap_context":false,"reason":"ordinary question does not depend on MCagent local corpus gaps"}',
            '{"tool":"direct_answer","reason":"simple arithmetic reply","collection_target":"","delivery_target":"human"}',
            "1+1=2. -- CrawlerAgent",
        ]
    )
    original_selector = web_server._selected_llm_client
    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake, "fake")  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            result = web_server._send_agent_message(
                make_temp_config(Path(tmp)),
                {"session_id": "fastapi-ask-crawler-no-job"},
                from_agent="User",
                content="Please ask CrawlerAgent to answer this: 1+1 equals what?",
                to_agent="MCagent",
                intent="user_chat",
                conversation_id="fastapi-ask-crawler-no-job",
            )
    finally:
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]

    assert_true("agent_message_answer", "1+1=2" in str(result.get("answer") or ""), str(result))
    assert_true("agent_message_no_job", not result.get("job") and not result.get("delegation"), str(result))
    assert_true("agent_message_response_visible", (result.get("receiver_response") or {}).get("agent") == "crawler_agent", str(result))
    traces = [(step.get("stage"), step.get("status")) for step in result.get("trace") or []]
    assert_true("agent_message_relay_trace", ("message", "agent_message_relayed") in traces, str(traces))
    assert_true("agent_message_preparing_trace", ("message", "agent_message_preparing") in traces, str(traces))
    assert_true("agent_message_waiting_trace", ("message", "agent_message_waiting_for_reply") in traces, str(traces))
    assert_true("agent_message_reply_trace", ("message", "agent_message_reply_received") in traces, str(traces))
    assert_true("agent_message_summary_trace", ("message", "agent_message_summary_ready") in traces, str(traces))
    assert_true("crawler_received_message", any(step.get("stage") == "message" and step.get("status") == "received" and (step.get("detail") or {}).get("to_agent") == "CrawlerAgent" for step in result.get("trace") or []), str(result.get("trace")))
    crawler_runtime = (result.get("receiver_response") or {}).get("agent_graph_runtime") or {}
    assert_true("crawler_graph_runtime", crawler_runtime.get("agent_graph") == "CrawlerAgentGraph", str(crawler_runtime))
    assert_true("crawler_direct_node", "crawler.graph_direct_answer_node" in crawler_runtime.get("visited_nodes", []), str(crawler_runtime))
    agent_runtime = result.get("agent_graph_runtime") or {}
    visited = agent_runtime.get("visited_nodes") or []
    assert_true("agent_message_graph_node", "mcagent.graph_agent_message_route" in visited, str(visited))
    assert_true("agent_message_legacy_not_visited", "mcagent.legacy_adapter" not in visited, str(visited))
    adapter = agent_runtime.get("runtime_adapter") or {}
    assert_true("agent_message_graph_adapter", adapter.get("adapter") == "graph_agent_message_route_executor", str(adapter))
    route_execution = agent_runtime.get("route_execution_contract") or {}
    assert_true("agent_message_graph_execution_fact", route_execution.get("route_execution_executed_by_graph") is True, str(route_execution))
    graph_runtime = result.get("graph_runtime") or {}
    assert_true("conversation_agent_message_node", "mcagent_graph.graph_agent_message_route" in graph_runtime.get("visited_nodes", []), str(graph_runtime))
    metadata = result.get("metadata") or {}
    assert_true("agent_message_metadata", ((metadata.get("agent_message") or {}).get("no_persistence") is True), str(metadata))
    assert_true("agent_message_final_summary", "From-Content-To" in str(result.get("answer") or "") and "1+1=2" in str(result.get("answer") or ""), str(result.get("answer") or ""))
    answer = str(result.get("answer") or "")
    assert_true("agent_message_summary_first_person", "From-Content-To" in answer and "CrawlerAgent" in answer, answer)
    assert_true("agent_message_no_third_person_template", "CrawlerAgent reply follows" not in answer and "MCagent already" not in answer, answer)


def test_mcagent_agent_message_response_passes_through_crawler_job_without_task_ticket_text() -> None:
    fake = SequencedClient(
        [
            '{"tool":"agent_message","reason":"ask CrawlerAgent to collect","to_agent":"CrawlerAgent","content":"璇疯ˉ榻愮己澶辫祫鏂?,"intent":"collection_request","delivery_target":"MCagent/RAG","metadata":{"tool":"collection_request","delivery_target":"MCagent/RAG"}}',
            '{"requires_gap_context":false,"reason":"pass-through test does not exercise local gap preparation"}',
        ]
    )
    original_selector = web_server._selected_llm_client
    original_send = web_server._send_agent_message

    def fake_send(config, payload, *, from_agent, content, to_agent, metadata=None, **kwargs):  # noqa: ANN001, ANN202, ARG001
        job = web_server.Job(id="pass-through-job", kind="crawler", title=content, status="queued", summary="queued")
        job.result = {"plan": {"topic": content, "delivery_target": "MCagent/RAG"}}
        return {
            "answer": "I am CrawlerAgent. I will choose my own collection actions and keep reporting progress.",
            "agent": "crawler_agent",
            "job": web_server._job_to_dict(job),
            "delegation": {"requested_by": "user_via_mcagent", "delivery_target": "MCagent/RAG", "task": content},
            "agent_message": web_server.make_agent_message("CrawlerAgent", "I will choose my own collection actions.", from_agent, requires_reply=False).to_dict(),
        }

    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake, "fake")  # type: ignore[assignment]
    web_server._send_agent_message = fake_send  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            result = web_server._send_agent_message(
                make_temp_config(Path(tmp)),
                {"session_id": "fastapi-agent-message-job-passthrough"},
                from_agent="User",
                content="鍙玞rawler鍘昏幏鍙栦綘缂虹殑璧勬枡",
                to_agent="MCagent",
                intent="user_chat",
                conversation_id="fastapi-agent-message-job-passthrough",
            )
    finally:
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        web_server._send_agent_message = original_send  # type: ignore[assignment]

    answer = str(result.get("answer") or "")
    assert_true("job_passed_through", (result.get("job") or {}).get("id") == "pass-through-job", str(result))
    assert_true("delegation_passed_through", (result.get("delegation") or {}).get("delivery_target") == "MCagent/RAG", str(result))
    assert_true("answer_no_task_ticket", "Task ID" not in answer and "task ticket" not in answer and "job card" not in answer, answer)


def test_mcagent_gap_collection_message_includes_local_gap_context() -> None:
    fake = SequencedClient(
        [
            json.dumps(
                {
                    "tool": "agent_message",
                    "reason": "MCagent should ask CrawlerAgent over the message bus.",
                    "to_agent": "CrawlerAgent",
                    "content": "Please collect what MCagent is missing.",
                    "intent": "delegate_collection",
                    "delivery_target": "MCagent/RAG",
                    "metadata": {"tool": "collection_request", "delivery_target": "MCagent/RAG"},
                }
            ),
            json.dumps(
                {
                    "requires_gap_context": True,
                    "focus": "local Minecraft modpack coverage gaps",
                    "reason": "The outgoing request depends on MCagent's local corpus gaps.",
                }
            ),
            json.dumps(
                {
                    "content": (
                        "I have checked my local corpus. I can see Utopian Journey and Closing Song, "
                        "but Craftoria and Prominence II still lack beginner route and quest guide evidence. "
                        "Please look for public sources for those gaps and decide your own collection steps."
                    ),
                    "gap_summary": "Craftoria and Prominence II lack beginner route and quest guide evidence.",
                    "reason": "MCagent converted objective local inventory into an AgentMessage.",
                }
            ),
        ]
    )
    captured: list[dict[str, object]] = []
    original_selector = web_server._selected_llm_client
    original_inventory = web_server._local_corpus_inventory_answer
    original_send = web_server._send_agent_message

    def fake_inventory(config, question):  # noqa: ANN001, ANN202, ARG001
        return {
            "answer": (
                "Inventory observation: local corpus has Utopian Journey and Closing Song internal evidence. "
                "Craftoria and Prominence II are visible as modpack entities, but beginner route, quest guide, "
                "version explanation, and reliable public source evidence are weak."
            ),
            "sources": [],
            "context": "",
            "agent": "mcagent_rag",
            "metadata": {
                "inventory_observation": {
                    "document_count": 10776,
                    "scanned_documents": 10776,
                    "entity_candidates": [
                        {"name": "Utopian Journey", "related_documents": 100},
                        {"name": "Closing Song", "related_documents": 80},
                        {"name": "Craftoria", "related_documents": 60},
                        {"name": "Prominence II", "related_documents": 50},
                    ],
                    "bucket_summary": [{"bucket": "modpack", "documents": 681}],
                }
            },
        }

    def fake_send(config, payload, *, from_agent, content, to_agent, metadata=None, **kwargs):  # noqa: ANN001, ANN202, ARG001
        captured.append({"payload": payload, "from_agent": from_agent, "content": content, "to_agent": to_agent, "metadata": metadata or {}})
        job = web_server.Job(id="gap-context-job", kind="crawler", title=content, status="queued", summary="queued")
        job.result = {"plan": {"topic": content, "delivery_target": "MCagent/RAG"}}
        return {
            "answer": "I am CrawlerAgent. I received MCagent's gap summary and will choose my own collection steps.",
            "agent": "crawler_agent",
            "job": web_server._job_to_dict(job),
        }

    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake, "fake")  # type: ignore[assignment]
    web_server._local_corpus_inventory_answer = fake_inventory  # type: ignore[assignment]
    web_server._send_agent_message = fake_send  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            result = web_server._chat_impl(
                make_temp_config(Path(tmp)),
                {
                    "session_id": "fastapi-gap-context-agent-message",
                    "agent": "mcagent_rag",
                    "question": "Ask CrawlerAgent to collect whatever MCagent is missing for the local modpack corpus.",
                },
            )
    finally:
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        web_server._local_corpus_inventory_answer = original_inventory  # type: ignore[assignment]
        web_server._send_agent_message = original_send  # type: ignore[assignment]

    assert_true("crawler_message_sent", len(captured) == 1, str(captured))
    sent = captured[0]
    sent_content = str(sent.get("content") or "")
    sent_metadata = sent.get("metadata") if isinstance(sent.get("metadata"), dict) else {}
    assert_true("sent_to_crawler", sent.get("to_agent") == "CrawlerAgent", str(sent))
    assert_true("sent_from_mcagent", sent.get("from_agent") == "MCagent", str(sent))
    assert_true("content_has_local_gap_context", "Craftoria" in sent_content and "Prominence II" in sent_content and "beginner route" in sent_content, sent_content)
    gap_context = sent_metadata.get("mcagent_gap_context") if isinstance(sent_metadata.get("mcagent_gap_context"), dict) else {}
    assert_true("gap_context_required", gap_context.get("required") is True, str(sent_metadata))
    assert_true("gap_context_inventory_metadata", ((gap_context.get("inventory_metadata") or {}).get("inventory_observation") or {}).get("document_count") == 10776, str(gap_context))
    payload_summary = (sent.get("payload") or {}).get("session_summary") if isinstance(sent.get("payload"), dict) else {}
    assert_true("payload_has_gap_context", isinstance(payload_summary, dict) and (payload_summary.get("mcagent_gap_context") or {}).get("required") is True, str(payload_summary))
    statuses = [(step.get("stage"), step.get("status")) for step in result.get("trace") or []]
    assert_true("inventory_scanning_trace", ("retrieve", "inventory_scanning") in statuses, str(statuses))
    assert_true("inventory_done_trace", ("retrieve", "inventory_done") in statuses, str(statuses))
    assert_true("handoff_prepared_trace", ("message", "mcagent_gap_handoff_prepared") in statuses, str(statuses))
    assert_true("final_answer_mentions_crawler_reply", "CrawlerAgent" in str(result.get("answer") or ""), str(result.get("answer") or ""))


def test_mcagent_inventory_agent_message_plan_continues_after_inventory() -> None:
    fake = SequencedClient(
        [
            json.dumps(
                {
                    "tool": "local_corpus_inventory",
                    "reason": "Inspect local corpus first, then tell CrawlerAgent the gaps.",
                    "collection_target": "collect missing modpack beginner guides",
                    "delivery_target": "MCagent/RAG",
                    "action_plan": [
                        {"step": 1, "tool": "local_corpus_inventory", "goal": "inspect local coverage"},
                        {"step": 2, "tool": "agent_message", "goal": "send the gaps to CrawlerAgent"},
                    ],
                }
            ),
            "Inventory answer prepared by MCagent. Craftoria and Prominence II lack beginner guide evidence.",
            json.dumps(
                {
                    "content": (
                        "I checked my local corpus. Utopian Journey and Closing Song have some internal evidence, "
                        "while Craftoria and Prominence II need public beginner guides. Please decide how to collect those sources."
                    ),
                    "gap_summary": "Craftoria and Prominence II need public beginner guides.",
                    "reason": "prepared MCagent gap handoff from inventory",
                }
            ),
            json.dumps({"handoff_brief": "MCagent sends the local gap summary to CrawlerAgent through AgentMessage.", "reason": "brief"}),
        ]
    )
    captured: list[dict[str, object]] = []
    original_selector = web_server._selected_llm_client
    original_inventory = web_server._local_corpus_inventory_answer
    original_send = web_server._send_agent_message

    def fake_inventory(config, question):  # noqa: ANN001, ANN202, ARG001
        return {
            "answer": "Inventory: Utopian Journey, Closing Song, Craftoria, and Prominence II are visible. Craftoria and Prominence II lack beginner guide evidence.",
            "sources": [],
            "context": "",
            "agent": "mcagent_rag",
            "metadata": {"inventory_observation": {"document_count": 10776, "entity_candidates": [{"name": "Craftoria"}, {"name": "Prominence II"}]}},
        }

    def fake_send(config, payload, *, from_agent, content, to_agent, metadata=None, **kwargs):  # noqa: ANN001, ANN202, ARG001
        captured.append({"payload": payload, "from_agent": from_agent, "content": content, "to_agent": to_agent, "metadata": metadata or {}})
        job = web_server.Job(id="planned-agent-message-job", kind="crawler", title=content, status="queued", summary="queued")
        job.result = {"plan": {"topic": content, "delivery_target": "MCagent/RAG"}}
        return {
            "answer": "I am CrawlerAgent. I received the gap message and will choose my own collection route.",
            "agent": "crawler_agent",
            "job": web_server._job_to_dict(job),
        }

    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake, "fake")  # type: ignore[assignment]
    web_server._local_corpus_inventory_answer = fake_inventory  # type: ignore[assignment]
    web_server._send_agent_message = fake_send  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            result = web_server._chat_impl(
                make_temp_config(Path(tmp)),
                {
                    "session_id": "fastapi-inventory-agent-message-plan",
                    "agent": "mcagent_rag",
                    "question": "Inspect local modpack gaps and ask CrawlerAgent to collect what is missing.",
                },
            )
    finally:
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        web_server._local_corpus_inventory_answer = original_inventory  # type: ignore[assignment]
        web_server._send_agent_message = original_send  # type: ignore[assignment]

    assert_true("planned_message_sent", len(captured) == 1, str(captured))
    sent = captured[0]
    assert_true("planned_sent_content", "Craftoria" in str(sent.get("content") or "") and "Prominence II" in str(sent.get("content") or ""), str(sent))
    assert_true("planned_sent_metadata_tool", (sent.get("metadata") or {}).get("tool") == "collection_request", str(sent))
    assert_true("planned_job_returned", (result.get("job") or {}).get("id") == "planned-agent-message-job", str(result))
    statuses = [(step.get("stage"), step.get("status")) for step in result.get("trace") or []]
    assert_true("planned_inventory_done", ("retrieve", "inventory_done") in statuses, str(statuses))
    assert_true("planned_handoff_prepared", ("message", "mcagent_gap_handoff_prepared") in statuses, str(statuses))
    assert_true("planned_workflow_trace", ("delegate", "planned_workflow") in statuses, str(statuses))


def test_mcagent_inventory_agent_message_plan_executes_in_graph_node() -> None:
    fake = SequencedClient(
        [
            json.dumps(
                {
                    "tool": "local_corpus_inventory",
                    "reason": "Inspect local corpus first, then tell CrawlerAgent the gaps.",
                    "collection_target": "collect missing modpack beginner guides",
                    "delivery_target": "MCagent/RAG",
                    "action_plan": [
                        {"step": 1, "tool": "local_corpus_inventory", "goal": "inspect local coverage"},
                        {"step": 2, "tool": "agent_message", "goal": "send the gaps to CrawlerAgent"},
                    ],
                }
            ),
            "Inventory answer prepared by MCagent. Craftoria and Prominence II lack beginner guide evidence.",
            json.dumps(
                {
                    "content": "I checked my local corpus. Craftoria and Prominence II need public beginner guides. Please decide how to collect those sources.",
                    "gap_summary": "Craftoria and Prominence II need public beginner guides.",
                    "reason": "prepared MCagent gap handoff from inventory",
                }
            ),
            json.dumps({"handoff_brief": "MCagent sends the local gap summary to CrawlerAgent through AgentMessage.", "reason": "brief"}),
        ]
    )
    captured: list[dict[str, object]] = []
    original_selector = web_server._selected_llm_client
    original_inventory = web_server._local_corpus_inventory_answer
    original_send = web_server._send_agent_message

    def fake_inventory(config, question):  # noqa: ANN001, ANN202, ARG001
        return {
            "answer": "Inventory: Craftoria and Prominence II lack beginner guide evidence.",
            "sources": [],
            "context": "",
            "agent": "mcagent_rag",
            "metadata": {"inventory_observation": {"document_count": 10776, "entity_candidates": [{"name": "Craftoria"}, {"name": "Prominence II"}]}},
        }

    def fake_send(config, payload, *, from_agent, content, to_agent, metadata=None, **kwargs):  # noqa: ANN001, ANN202, ARG001
        captured.append({"content": content, "to_agent": to_agent, "metadata": metadata or {}})
        job = web_server.Job(id="graph-planned-agent-message-job", kind="crawler", title=content, status="queued", summary="queued")
        job.result = {"plan": {"topic": content, "delivery_target": "MCagent/RAG"}}
        return {
            "answer": "I am CrawlerAgent. I received the gap message and will choose my own collection route.",
            "agent": "crawler_agent",
            "job": web_server._job_to_dict(job),
        }

    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake, "fake")  # type: ignore[assignment]
    web_server._local_corpus_inventory_answer = fake_inventory  # type: ignore[assignment]
    web_server._send_agent_message = fake_send  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            result = original_send(
                make_temp_config(Path(tmp)),
                {"session_id": "fastapi-inventory-agent-message-plan-graph"},
                from_agent="User",
                content="Inspect local modpack gaps and ask CrawlerAgent to collect what is missing.",
                to_agent="MCagent",
                intent="user_chat",
                conversation_id="fastapi-inventory-agent-message-plan-graph",
            )
    finally:
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        web_server._local_corpus_inventory_answer = original_inventory  # type: ignore[assignment]
        web_server._send_agent_message = original_send  # type: ignore[assignment]

    assert_true("graph_planned_message_sent", len(captured) == 1, str(captured))
    agent_runtime = result.get("agent_graph_runtime") or {}
    visited = agent_runtime.get("visited_nodes") or []
    assert_true("graph_planned_node", "mcagent.graph_mcagent_inventory_planned_workflow" in visited, str(visited))
    assert_true("graph_planned_legacy_not_visited", "mcagent.legacy_adapter" not in visited, str(visited))
    adapter = agent_runtime.get("runtime_adapter") or {}
    assert_true("graph_planned_adapter", adapter.get("adapter") == "graph_mcagent_inventory_planned_workflow_executor", str(adapter))
    route_execution = agent_runtime.get("route_execution_contract") or {}
    assert_true("graph_planned_executed", route_execution.get("route_execution_executed_by_graph") is True, str(route_execution))
    assert_true("graph_planned_job", (result.get("job") or {}).get("id") == "graph-planned-agent-message-job", str(result))


def test_mcagent_gap_context_timeout_fallback_is_readable_first_person() -> None:
    class TimeoutClient(SequencedClient):
        def chat(self, messages, *, temperature=None, max_tokens=None):  # noqa: ANN001, ANN201, ARG002
            self.calls.append({"messages": messages, "temperature": temperature, "max_tokens": max_tokens})
            if len(self.calls) == 1:
                return json.dumps(
                    {
                        "tool": "agent_message",
                        "reason": "MCagent should ask CrawlerAgent over the message bus.",
                        "to_agent": "CrawlerAgent",
                        "content": "Please collect what MCagent is missing.",
                        "intent": "crawler_collection",
                        "delivery_target": "MCagent/RAG",
                    }
                )
            if len(self.calls) == 2:
                return json.dumps({"requires_gap_context": True, "focus": "modpack gaps", "reason": "needs context"})
            raise RuntimeError("handoff generation timed out")

    fake = TimeoutClient([])
    captured: list[dict[str, object]] = []
    original_selector = web_server._selected_llm_client
    original_inventory = web_server._local_corpus_inventory_answer
    original_send = web_server._send_agent_message

    def fake_inventory(config, question):  # noqa: ANN001, ANN202, ARG001
        return {
            "answer": "local_corpus_inventory raw observation with many details that should not be dumped wholesale",
            "sources": [],
            "context": "",
            "agent": "mcagent_rag",
            "metadata": {
                "inventory_observation": {
                    "document_count": 10776,
                    "entity_candidates": [
                        {"name": "VanillaEra:FaresChron"},
                        {"name": "Craftoria"},
                        {"name": "Prominence II"},
                        {"name": "Utopian Journey"},
                        {"name": "Closing Song"},
                    ],
                }
            },
        }

    def fake_send(config, payload, *, from_agent, content, to_agent, metadata=None, **kwargs):  # noqa: ANN001, ANN202, ARG001
        captured.append({"content": content, "metadata": metadata or {}, "to_agent": to_agent})
        return {"answer": "I am CrawlerAgent. Received.", "agent": "crawler_agent"}

    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake, "fake")  # type: ignore[assignment]
    web_server._local_corpus_inventory_answer = fake_inventory  # type: ignore[assignment]
    web_server._send_agent_message = fake_send  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            result = web_server._chat_impl(
                make_temp_config(Path(tmp)),
                {
                    "session_id": "fastapi-gap-context-timeout-fallback",
                    "agent": "mcagent_rag",
                    "question": "Ask CrawlerAgent to collect what MCagent is missing.",
                },
            )
    finally:
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        web_server._local_corpus_inventory_answer = original_inventory  # type: ignore[assignment]
        web_server._send_agent_message = original_send  # type: ignore[assignment]

    assert_true("fallback_message_sent", len(captured) == 1, str(captured))
    content = str(captured[0].get("content") or "")
    assert_true("fallback_first_person", "MCagent" in content, content)
    assert_true("fallback_names", "Craftoria" in content and "Prominence II" in content and "Utopian Journey" in content, content)
    assert_true("fallback_not_raw_dump", "raw observation with many details" not in content, content)
    statuses = [(step.get("stage"), step.get("status")) for step in result.get("trace") or []]
    assert_true("fallback_trace_failed", ("message", "mcagent_gap_handoff_prepare_failed") in statuses, str(statuses))


def test_agent_selected_temporary_extract_bypasses_legacy_delivery() -> None:
    fake = SequencedClient(
        [
            '{"tool":"temporary_extract","reason":"read one public page without saving","collection_target":"https://example.com","delivery_target":"human"}',
            '{"proceed":true,"tool":"temporary_extract","reason":"confirmed temporary read"}',
            "temporary extract summary",
        ]
    )
    original_selector = web_server._selected_llm_client
    original_delivery = web_server._deliver_agent_message
    original_extract_service = web_server.CrawlerTemporaryExtractService

    class FakeExtractResult:
        url = "https://example.com"
        status_code = 200
        content_type = "text/html"
        text_chars = 42

        def to_response(self, agent: str) -> dict[str, object]:
            return {
                "answer": "temporary extract summary",
                "sources": [{"title": "Example", "url": self.url}],
                "context": "example page text",
                "agent": agent,
                "temporary_extract": {"saved_to_local": False, "url": self.url},
            }

    class FakeExtractService:
        def run(self, *, question, collection_target, summarize, review_summarize, choose_url):  # noqa: ANN001, ANN201
            assert_true("temporary_extract_question_passed", bool(question), str(question))
            assert_true("temporary_extract_target_passed", collection_target == "https://example.com", str(collection_target))
            return FakeExtractResult()

    def forbidden_delivery(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("legacy delivery should not run for graph-executed temporary_extract")

    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake, "fake")  # type: ignore[assignment]
    web_server._deliver_agent_message = forbidden_delivery  # type: ignore[assignment]
    web_server.CrawlerTemporaryExtractService = FakeExtractService  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            result = web_server._send_agent_message(
                make_temp_config(Path(tmp)),
                {"session_id": "fastapi-temporary-extract-graph"},
                from_agent="User",
                content="read https://example.com once",
                to_agent="CrawlerAgent",
                intent="user_chat",
                conversation_id="fastapi-temporary-extract-graph",
            )
    finally:
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        web_server._deliver_agent_message = original_delivery  # type: ignore[assignment]
        web_server.CrawlerTemporaryExtractService = original_extract_service  # type: ignore[assignment]

    traces = [(step.get("stage"), step.get("status")) for step in result.get("trace") or []]
    assert_true("temporary_extract_confirmed", ("extract", "next_step_confirmed") in traces, str(traces))
    assert_true("temporary_extract_extracted", ("extract", "temporary_url_extracted") in traces, str(traces))
    assert_true("temporary_extract_response", result.get("answer") == "temporary extract summary", str(result))
    assert_true("temporary_extract_no_job", not result.get("job") and not result.get("delegation"), str(result))
    assert_true("temporary_extract_saved_false", (result.get("temporary_extract") or {}).get("saved_to_local") is False, str(result))
    agent_runtime = result.get("agent_graph_runtime") or {}
    visited = agent_runtime.get("visited_nodes") or []
    assert_true("graph_temporary_extract_node", "crawler.graph_temporary_extract_node" in visited, str(visited))
    assert_true("legacy_not_visited", "crawler.legacy_adapter" not in visited, str(visited))
    adapter = agent_runtime.get("runtime_adapter") or {}
    assert_true("graph_temporary_extract_adapter", adapter.get("adapter") == "graph_temporary_extract_node_executor", str(adapter))
    assert_true("graph_temporary_extract_saved_false", adapter.get("saved_to_local") is False, str(adapter))
    route_execution = agent_runtime.get("route_execution_contract") or {}
    assert_true("graph_temporary_extract_execution_fact", route_execution.get("route_execution_executed_by_graph") is True, str(route_execution))
    assert_true("graph_temporary_extract_stage_fact", (route_execution.get("trace_facts") or {}).get("has_extract_trace") is True, str(route_execution))
    assert_true("graph_temporary_extract_result_fact", (route_execution.get("result_facts") or {}).get("temporary_extract_present") is True, str(route_execution))
    legacy_surface = agent_runtime.get("legacy_handler_surface_contract") or {}
    assert_true("graph_temporary_extract_surface_fact", legacy_surface.get("handler_executed_by_contract") is True, str(legacy_surface))
    assert_true("graph_temporary_extract_surface_not_legacy", legacy_surface.get("legacy_handlers_still_run_in_adapter") is False, str(legacy_surface))
    graph_runtime = result.get("graph_runtime") or {}
    assert_true("conversation_temporary_extract_node", "crawler_graph.graph_temporary_extract_node" in graph_runtime.get("visited_nodes", []), str(graph_runtime))


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


def test_crawler_planned_workflow_executes_in_graph_node() -> None:
    fake = SequencedClient(
        [
            json.dumps(
                {
                    "tool": "planned_workflow",
                    "reason": "I received an AgentMessage asking me to collect missing modpack guides, so I choose my own collection workflow.",
                    "collection_target": "Find public beginner guides for Craftoria and Prominence II.",
                    "delivery_target": "MCagent/RAG",
                    "action_plan": [
                        {"step": 1, "tool": "delegate_crawler", "goal": "start the CrawlerAgent-owned collection workflow"},
                    ],
                }
            ),
            json.dumps({"proceed": True, "tool": "planned_workflow", "reason": "confirmed CrawlerAgent-selected workflow"}),
            json.dumps({"handoff_brief": "I will collect public beginner-guide evidence for MCagent/RAG.", "reason": "CrawlerAgent selected collection"}),
        ]
    )
    original_selector = web_server._selected_llm_client
    original_delivery = web_server._deliver_agent_message
    original_start = web_server._start_crawler_job_from_crawler_tool
    calls: list[dict[str, object]] = []

    def forbidden_delivery(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("legacy delivery should not run for graph-executed CrawlerAgent planned_workflow")

    def fake_start(config: AppConfig, payload: dict[str, object], question: str, plan: dict[str, object] | None = None):  # noqa: ARG001
        calls.append({"payload": payload, "question": question, "plan": plan})
        job = web_server.Job(id="graph-crawler-planned-job", kind="crawler", title=question, status="queued", summary="queued")
        job.result = {"plan": {"topic": question, "delivery_target": payload.get("delivery_target")}}
        return job, True

    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake, "fake")  # type: ignore[assignment]
    web_server._deliver_agent_message = forbidden_delivery  # type: ignore[assignment]
    web_server._start_crawler_job_from_crawler_tool = fake_start  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            result = web_server._send_agent_message(
                make_temp_config(Path(tmp)),
                {"session_id": "fastapi-crawler-planned-graph", "model": "fake-model", "delivery_target": "MCagent/RAG"},
                from_agent="MCagent",
                content="I found gaps in Craftoria and Prominence II beginner-guide evidence. Please decide how to collect public sources.",
                to_agent="CrawlerAgent",
                intent="collection_request",
                conversation_id="fastapi-crawler-planned-graph",
                metadata={"tool": "collection_request", "delivery_target": "MCagent/RAG", "requested_by": "mcagent"},
            )
    finally:
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        web_server._deliver_agent_message = original_delivery  # type: ignore[assignment]
        web_server._start_crawler_job_from_crawler_tool = original_start  # type: ignore[assignment]

    assert_true("crawler_job_started_once", len(calls) == 1, str(calls))
    call_payload = calls[0].get("payload") if isinstance(calls[0].get("payload"), dict) else {}
    assert_true("crawler_agent_owns_job_start", call_payload.get("agent") == "crawler_agent", str(call_payload))
    message = call_payload.get("agent_message") if isinstance(call_payload.get("agent_message"), dict) else {}
    assert_true("crawler_job_requires_agent_message", message.get("to_agent") == "CrawlerAgent", str(message))
    metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    assert_true("crawler_job_selected_delegate_tool", metadata.get("tool") == "delegate_crawler", str(message))
    assert_true("crawler_planned_job", (result.get("job") or {}).get("id") == "graph-crawler-planned-job", str(result))
    traces = [(step.get("stage"), step.get("status")) for step in result.get("trace") or []]
    assert_true("crawler_collection_request_observed", ("message", "collection_request_received_for_agent_decision") in traces, str(traces))
    assert_true("crawler_selected_delegate_step", ("plan", "executing_agent_selected_step") in traces, str(traces))
    agent_runtime = result.get("agent_graph_runtime") or {}
    visited = agent_runtime.get("visited_nodes") or []
    assert_true("graph_crawler_planned_node", "crawler.graph_crawler_planned_workflow" in visited, str(visited))
    assert_true("crawler_legacy_not_visited", "crawler.legacy_adapter" not in visited, str(visited))
    adapter = agent_runtime.get("runtime_adapter") or {}
    assert_true("graph_crawler_planned_adapter", adapter.get("adapter") == "graph_crawler_planned_workflow_executor", str(adapter))
    assert_true("graph_crawler_planned_boundary", "does not choose sources" in str(adapter.get("objective_boundary") or ""), str(adapter))
    route_execution = agent_runtime.get("route_execution_contract") or {}
    assert_true("graph_crawler_planned_execution_fact", route_execution.get("route_execution_executed_by_graph") is True, str(route_execution))
    assert_true("graph_crawler_planned_job_fact", (route_execution.get("result_facts") or {}).get("job_present") is True, str(route_execution))
    graph_runtime = result.get("graph_runtime") or {}
    assert_true("conversation_crawler_planned_node", "crawler_graph.graph_crawler_planned_workflow" in graph_runtime.get("visited_nodes", []), str(graph_runtime))


def test_crawler_mcagent_context_route_executes_in_graph_node() -> None:
    fake = SequencedClient(
        [
            json.dumps(
                {
                    "tool": "mcagent_context",
                    "reason": "I need MCagent local context before deciding whether external collection is needed.",
                    "collection_target": "Craftoria modpack",
                    "delivery_target": "CrawlerAgent",
                }
            ),
            json.dumps({"proceed": True, "tool": "mcagent_context", "reason": "confirmed CrawlerAgent-selected context request"}),
        ]
    )
    original_selector = web_server._selected_llm_client
    original_delivery = web_server._deliver_agent_message
    original_retriever = web_server.Retriever
    original_rag_retrieve = web_server.RagRetrievalService.retrieve

    def forbidden_delivery(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("legacy delivery should not run for graph-executed CrawlerAgent mcagent_context route")

    class FakeRetriever:
        def __init__(self, _config: AppConfig) -> None:
            pass

        def search(self, query: str, *, top_k: int, session_summary: dict[str, object] | None = None):  # noqa: ANN201, ARG002
            return [
                SearchResult(
                    rank=1,
                    score=0.91,
                    chunk_id=201,
                    document_id=601,
                    chunk_index=0,
                    title="Craftoria local gap note",
                    source_path="D:/case/data/crawler_exports/mcagent_context/craftoria_gap.md",
                    url=None,
                    text="Craftoria local gap note: local internals exist, but public beginner guide evidence is missing.",
                    metadata={"source": "mcagent_context", "project": "Craftoria"},
                )
            ]

    def forbidden_rag(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("CrawlerAgent mcagent_context route should send AgentMessage, not answer with chat-turn RAG")

    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake, "fake")  # type: ignore[assignment]
    web_server._deliver_agent_message = forbidden_delivery  # type: ignore[assignment]
    web_server.Retriever = FakeRetriever  # type: ignore[assignment]
    web_server.RagRetrievalService.retrieve = forbidden_rag  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            result = web_server._send_agent_message(
                make_temp_config(Path(tmp)),
                {"session_id": "fastapi-crawler-mcagent-context-graph", "model": "fake-model"},
                from_agent="User",
                content="Ask MCagent what Craftoria local evidence and gaps exist.",
                to_agent="CrawlerAgent",
                intent="user_chat",
                conversation_id="fastapi-crawler-mcagent-context-graph",
            )
    finally:
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        web_server._deliver_agent_message = original_delivery  # type: ignore[assignment]
        web_server.Retriever = original_retriever  # type: ignore[assignment]
        web_server.RagRetrievalService.retrieve = original_rag_retrieve  # type: ignore[assignment]

    answer = str(result.get("answer") or "")
    assert_true("crawler_mcagent_context_answer", "MCagent" in answer and "Craftoria local gap note" in answer, answer)
    assert_true("crawler_mcagent_context_no_job", not result.get("job") and not result.get("delegation"), str(result))
    traces = [(step.get("stage"), step.get("status")) for step in result.get("trace") or []]
    assert_true("crawler_mcagent_context_selected_trace", ("decide", "mcagent_context_selected") in traces, str(traces))
    assert_true("crawler_mcagent_context_message_trace", ("message", "agent_message_relayed") in traces, str(traces))
    agent_runtime = result.get("agent_graph_runtime") or {}
    visited = agent_runtime.get("visited_nodes") or []
    assert_true("crawler_mcagent_context_graph_node", "crawler.graph_crawler_mcagent_context_route" in visited, str(visited))
    assert_true("crawler_mcagent_context_legacy_not_visited", "crawler.legacy_adapter" not in visited, str(visited))
    adapter = agent_runtime.get("runtime_adapter") or {}
    assert_true("crawler_mcagent_context_adapter", adapter.get("adapter") == "graph_crawler_mcagent_context_route_executor", str(adapter))
    route_execution = agent_runtime.get("route_execution_contract") or {}
    assert_true("crawler_mcagent_context_execution_graph", route_execution.get("route_execution_executed_by_graph") is True, str(route_execution))
    graph_runtime = result.get("graph_runtime") or {}
    assert_true("conversation_crawler_mcagent_context_node", "crawler_graph.graph_crawler_mcagent_context_route" in graph_runtime.get("visited_nodes", []), str(graph_runtime))


def test_crawler_delegate_route_executes_in_graph_node() -> None:
    fake = SequencedClient(
        [
            json.dumps(
                {
                    "tool": "delegate_crawler",
                    "reason": "I received an AgentMessage asking me to collect missing modpack evidence, so I choose my collection tool.",
                    "collection_target": "Collect beginner guides for Craftoria and Prominence II.",
                    "delivery_target": "MCagent/RAG",
                }
            ),
            json.dumps({"proceed": True, "tool": "delegate_crawler", "reason": "confirmed CrawlerAgent-selected delegate"}),
            json.dumps({"handoff_brief": "I will collect public beginner-guide evidence for MCagent/RAG.", "reason": "CrawlerAgent selected collection"}),
        ]
    )
    original_selector = web_server._selected_llm_client
    original_delivery = web_server._deliver_agent_message
    original_start = web_server._start_crawler_job_from_crawler_tool
    calls: list[dict[str, object]] = []

    def forbidden_delivery(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("legacy delivery should not run for graph-executed CrawlerAgent delegate_crawler route")

    def fake_start(config: AppConfig, payload: dict[str, object], question: str, plan: dict[str, object] | None = None):  # noqa: ARG001
        calls.append({"payload": payload, "question": question, "plan": plan})
        job = web_server.Job(id="graph-crawler-delegate-job", kind="crawler", title=question, status="queued", summary="queued")
        job.result = {"plan": {"topic": question, "delivery_target": payload.get("delivery_target")}}
        return job, True

    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake, "fake")  # type: ignore[assignment]
    web_server._deliver_agent_message = forbidden_delivery  # type: ignore[assignment]
    web_server._start_crawler_job_from_crawler_tool = fake_start  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            result = web_server._send_agent_message(
                make_temp_config(Path(tmp)),
                {"session_id": "fastapi-crawler-delegate-graph", "model": "fake-model", "delivery_target": "MCagent/RAG"},
                from_agent="MCagent",
                content="I found gaps in Craftoria and Prominence II beginner-guide evidence. Please decide how to collect public sources.",
                to_agent="CrawlerAgent",
                intent="collection_request",
                conversation_id="fastapi-crawler-delegate-graph",
                metadata={"tool": "collection_request", "delivery_target": "MCagent/RAG", "requested_by": "mcagent"},
            )
    finally:
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        web_server._deliver_agent_message = original_delivery  # type: ignore[assignment]
        web_server._start_crawler_job_from_crawler_tool = original_start  # type: ignore[assignment]

    assert_true("crawler_delegate_job_started_once", len(calls) == 1, str(calls))
    call_payload = calls[0].get("payload") if isinstance(calls[0].get("payload"), dict) else {}
    assert_true("crawler_delegate_agent_owns_job_start", call_payload.get("agent") == "crawler_agent", str(call_payload))
    message = call_payload.get("agent_message") if isinstance(call_payload.get("agent_message"), dict) else {}
    assert_true("crawler_delegate_job_requires_agent_message", message.get("to_agent") == "CrawlerAgent", str(message))
    metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    assert_true("crawler_delegate_job_selected_tool", metadata.get("tool") == "delegate_crawler", str(message))
    assert_true("crawler_delegate_job", (result.get("job") or {}).get("id") == "graph-crawler-delegate-job", str(result))
    traces = [(step.get("stage"), step.get("status")) for step in result.get("trace") or []]
    assert_true("crawler_delegate_collection_request_observed", ("message", "collection_request_received_for_agent_decision") in traces, str(traces))
    assert_true("crawler_delegate_next_step_confirmed", ("delegate", "next_step_confirmed") in traces, str(traces))
    agent_runtime = result.get("agent_graph_runtime") or {}
    visited = agent_runtime.get("visited_nodes") or []
    assert_true("graph_crawler_delegate_node", "crawler.graph_crawler_delegate_route" in visited, str(visited))
    assert_true("crawler_delegate_legacy_not_visited", "crawler.legacy_adapter" not in visited, str(visited))
    adapter = agent_runtime.get("runtime_adapter") or {}
    assert_true("graph_crawler_delegate_adapter", adapter.get("adapter") == "graph_crawler_delegate_route_executor", str(adapter))
    assert_true("graph_crawler_delegate_boundary", "does not choose sources" in str(adapter.get("objective_boundary") or ""), str(adapter))
    route_execution = agent_runtime.get("route_execution_contract") or {}
    assert_true("graph_crawler_delegate_execution_fact", route_execution.get("route_execution_executed_by_graph") is True, str(route_execution))
    assert_true("graph_crawler_delegate_job_fact", (route_execution.get("result_facts") or {}).get("job_present") is True, str(route_execution))
    graph_runtime = result.get("graph_runtime") or {}
    assert_true("conversation_crawler_delegate_node", "crawler_graph.graph_crawler_delegate_route" in graph_runtime.get("visited_nodes", []), str(graph_runtime))


def test_mcagent_inventory_plan_cannot_smuggle_delegate_tool() -> None:
    fake = SequencedClient(
        [
            '{"tool":"local_corpus_inventory","reason":"inspect then delegate","action_plan":[{"step":1,"tool":"local_corpus_inventory","goal":"inspect local coverage"},{"step":2,"tool":"delegate_crawler","goal":"collect missing evidence"}],"collection_target":"collect missing evidence","delivery_target":"MCagent/RAG"}',
        ]
    )
    original_selector = web_server._selected_llm_client
    original_delivery = web_server._deliver_agent_message
    original_inventory = web_server._local_corpus_inventory_answer
    calls: list[dict[str, object]] = []

    def fake_delivery(_config: AppConfig, payload: dict[str, object], emit=None) -> dict[str, object]:  # noqa: ANN001, ARG001
        decision = payload.get("_graph_route_decision") if isinstance(payload.get("_graph_route_decision"), dict) else {}
        calls.append({"route_intent": decision.get("route_intent"), "action_plan": decision.get("action_plan")})
        return {"answer": "legacy inventory path used", "sources": [], "context": "", "agent": "mcagent_rag"}

    def fake_inventory(_config: AppConfig, _question: str) -> dict[str, object]:
        return {
            "answer": "Local inventory: delegate step was not exposed to MCagent tools.",
            "sources": [{"title": "local inventory", "document_id": 1}],
            "context": "inventory context",
            "agent": "mcagent_rag",
        }

    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake, "fake")  # type: ignore[assignment]
    web_server._deliver_agent_message = fake_delivery  # type: ignore[assignment]
    web_server._local_corpus_inventory_answer = fake_inventory  # type: ignore[assignment]
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
        web_server._local_corpus_inventory_answer = original_inventory  # type: ignore[assignment]

    assert_true("legacy_delivery_not_called", calls == [], str(calls))
    assert_true("inventory_answer", "delegate step was not exposed" in str(result.get("answer") or ""), str(result))
    agent_runtime = result.get("agent_graph_runtime") or {}
    visited = agent_runtime.get("visited_nodes") or []
    assert_true("graph_inventory_node", "mcagent.graph_local_corpus_inventory_route" in visited, str(visited))
    assert_true("legacy_inventory_not_visited", "mcagent.legacy_adapter" not in visited, str(visited))
    adapter = agent_runtime.get("runtime_adapter") or {}
    assert_true("graph_inventory_adapter", adapter.get("adapter") == "graph_local_corpus_inventory_route_executor", str(adapter))
    graph_runtime = result.get("graph_runtime") or {}
    assert_true("conversation_graph_inventory_node", "mcagent_graph.graph_local_corpus_inventory_route" in graph_runtime.get("visited_nodes", []), str(graph_runtime))


def test_mcagent_inventory_planned_workflow_without_handoff_uses_inventory_node() -> None:
    fake = SequencedClient(
        [
            json.dumps(
                {
                    "tool": "planned_workflow",
                    "reason": "The user asks for whole local corpus coverage plus beginner guidance, so I should inspect inventory first.",
                    "action_plan": [
                        {"step": 1, "tool": "local_corpus_inventory", "goal": "Inspect local modpack corpus coverage."},
                        {"step": 2, "tool": "local_rag_search", "goal": "Use local evidence for beginner guidance after inventory."},
                        {"step": 3, "tool": "final_answer_llm", "goal": "Summarize for the user."},
                    ],
                }
            ),
            json.dumps({"proceed": True, "tool": "planned_workflow", "reason": "confirmed compound local workflow"}),
            "I can answer from the local inventory observation.",
        ]
    )
    original_selector = web_server._selected_llm_client
    original_delivery = web_server._deliver_agent_message
    original_inventory = web_server._local_corpus_inventory_answer
    original_retriever = web_server.RagRetrievalService.retrieve

    def forbidden_delivery(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("legacy delivery should not run for graph-executed inventory workflow")

    def fake_inventory(_config: AppConfig, _question: str) -> dict[str, object]:
        return {
            "answer": "Inventory includes Craftoria, Prominence II, Utopian Journey, Closing Song, and VanillaEra.",
            "sources": [{"title": "local inventory", "document_id": 1}],
            "context": "inventory context",
            "agent": "mcagent_rag",
        }

    def forbidden_rag(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("inventory-first planned workflow should not fall into slow RAG evidence route")

    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake, "fake")  # type: ignore[assignment]
    web_server._deliver_agent_message = forbidden_delivery  # type: ignore[assignment]
    web_server._local_corpus_inventory_answer = fake_inventory  # type: ignore[assignment]
    web_server.RagRetrievalService.retrieve = forbidden_rag  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            result = web_server._send_agent_message(
                make_temp_config(Path(tmp)),
                {"session_id": "fastapi-inventory-planned-local"},
                from_agent="User",
                content="What local modpacks exist and how should beginners play them",
                to_agent="MCagent",
                intent="user_chat",
                conversation_id="fastapi-inventory-planned-local",
            )
    finally:
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        web_server._deliver_agent_message = original_delivery  # type: ignore[assignment]
        web_server._local_corpus_inventory_answer = original_inventory  # type: ignore[assignment]
        web_server.RagRetrievalService.retrieve = original_retriever  # type: ignore[assignment]

    answer = str(result.get("answer") or "")
    assert_true("inventory_answer", "Craftoria" in answer and "Utopian Journey" in answer, answer)
    agent_runtime = result.get("agent_graph_runtime") or {}
    visited = agent_runtime.get("visited_nodes") or []
    assert_true("inventory_graph_node", "mcagent.graph_local_corpus_inventory_route" in visited, str(visited))
    assert_true("inventory_no_legacy", "mcagent.legacy_adapter" not in visited, str(visited))
    assert_true("inventory_no_rag_node", "mcagent.graph_rag_answer_route" not in visited, str(visited))
    route_decision = agent_runtime.get("route_decision") or {}
    assert_true("planned_workflow_normalized_to_inventory", route_decision.get("route_intent") == "local_corpus_inventory", str(route_decision))
    assert_true("original_route_preserved", route_decision.get("original_route_intent") == "answer", str(route_decision))


def test_mcagent_natural_language_inventory_plan_uses_inventory_node() -> None:
    fake = SequencedClient(
        [
            json.dumps(
                {
                    "tool": "planned_workflow",
                    "reason": "The user asks for a corpus inventory and beginner overview.",
                    "action_plan": [
                        {"step": 1, "goal": "盘点本地知识库中收录的所有 Minecraft 整合包，输出名称、来源等基本情况"},
                        {"step": 2, "goal": "结合整合包清单和新手玩法证据，生成面向用户的最终回答"},
                    ],
                }
            ),
            json.dumps({"proceed": True, "tool": "planned_workflow", "reason": "confirmed compound local workflow"}),
            "I can answer from the local inventory observation.",
        ]
    )
    original_selector = web_server._selected_llm_client
    original_delivery = web_server._deliver_agent_message
    original_inventory = web_server._local_corpus_inventory_answer
    original_retriever = web_server.RagRetrievalService.retrieve

    def forbidden_delivery(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("legacy delivery should not run for graph-executed natural-language inventory workflow")

    def fake_inventory(_config: AppConfig, _question: str) -> dict[str, object]:
        return {
            "answer": "Inventory includes Craftoria, Prominence II, Utopian Journey, Closing Song, and VanillaEra.",
            "sources": [{"title": "local inventory", "document_id": 1}],
            "context": "inventory context",
            "agent": "mcagent_rag",
        }

    def forbidden_rag(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("natural-language inventory plan should not fall into slow RAG evidence route")

    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake, "fake")  # type: ignore[assignment]
    web_server._deliver_agent_message = forbidden_delivery  # type: ignore[assignment]
    web_server._local_corpus_inventory_answer = fake_inventory  # type: ignore[assignment]
    web_server.RagRetrievalService.retrieve = forbidden_rag  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            result = web_server._send_agent_message(
                make_temp_config(Path(tmp)),
                {"session_id": "fastapi-inventory-natural-plan"},
                from_agent="User",
                content="本地有哪些整合包 新手该怎么玩",
                to_agent="MCagent",
                intent="user_chat",
                conversation_id="fastapi-inventory-natural-plan",
            )
    finally:
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        web_server._deliver_agent_message = original_delivery  # type: ignore[assignment]
        web_server._local_corpus_inventory_answer = original_inventory  # type: ignore[assignment]
        web_server.RagRetrievalService.retrieve = original_retriever  # type: ignore[assignment]

    answer = str(result.get("answer") or "")
    assert_true("inventory_answer", "Craftoria" in answer and "Utopian Journey" in answer, answer)
    assert_true("plan_not_in_chat_answer", "执行计划：" not in answer, answer)
    agent_runtime = result.get("agent_graph_runtime") or {}
    visited = agent_runtime.get("visited_nodes") or []
    assert_true("inventory_graph_node", "mcagent.graph_local_corpus_inventory_route" in visited, str(visited))
    assert_true("inventory_no_rag_node", "mcagent.graph_rag_answer_route" not in visited, str(visited))
    route_decision = agent_runtime.get("route_decision") or {}
    steps = route_decision.get("action_plan") or []
    assert_true("natural_plan_step_normalized", any((step or {}).get("tool") == "local_corpus_inventory" for step in steps), str(steps))


def main() -> int:
    test_fastapi_core_routes()
    test_fastapi_preview_limits_tolerate_malformed_payload_values()
    test_fastapi_sse_chat_shape()
    test_fastapi_agent_message_endpoint_dispatches()
    test_agent_selected_status_bypasses_legacy_delivery()
    test_agent_selected_direct_answer_bypasses_legacy_delivery()
    test_mcagent_rag_answer_executes_in_graph_node()
    test_mcagent_local_rag_search_executes_in_graph_node()
    test_mcagent_local_rag_empty_result_executes_in_graph_node()
    test_mcagent_context_request_executes_in_graph_node()
    test_mcagent_direct_answer_protocol_explains_crawler_contact_as_agent_message()
    test_mcagent_can_message_crawler_agent_without_collection_side_effect()
    test_mcagent_agent_message_response_passes_through_crawler_job_without_task_ticket_text()
    test_mcagent_gap_collection_message_includes_local_gap_context()
    test_mcagent_inventory_agent_message_plan_continues_after_inventory()
    test_mcagent_inventory_agent_message_plan_executes_in_graph_node()
    test_mcagent_gap_context_timeout_fallback_is_readable_first_person()
    test_agent_selected_temporary_extract_bypasses_legacy_delivery()
    test_agent_selected_crawler_audit_bypasses_legacy_delivery()
    test_agent_selected_safe_local_inventory_bypasses_legacy_delivery()
    test_agent_selected_router_error_bypasses_legacy_delivery()
    test_crawler_planned_workflow_executes_in_graph_node()
    test_crawler_mcagent_context_route_executes_in_graph_node()
    test_crawler_delegate_route_executes_in_graph_node()
    test_mcagent_inventory_plan_cannot_smuggle_delegate_tool()
    test_mcagent_inventory_planned_workflow_without_handoff_uses_inventory_node()
    test_mcagent_natural_language_inventory_plan_uses_inventory_node()
    print("FASTAPI BACKEND SCENARIOS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
