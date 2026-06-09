from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
from typing import Any


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
from mcagent.graphs import dispatch_agent_message_graph  # noqa: E402
from mcagent.graphs import runtime as graph_runtime_module  # noqa: E402
from mcagent.graphs.crawler_job import run_crawler_job_graph  # noqa: E402
from mcagent.session_state import DEFAULT_SESSION_STORE  # noqa: E402
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


def test_conversation_graph_routes_only_by_message_target() -> None:
    calls: list[dict[str, Any]] = []

    def legacy(config: AppConfig, payload: dict[str, Any], emit: Any | None = None) -> dict[str, Any]:  # noqa: ARG001
        calls.append(dict(payload))
        return {
            "answer": f"delivered to {payload.get('agent')}",
            "agent": payload.get("agent"),
            "sources": [],
            "context": "",
        }

    with tempfile.TemporaryDirectory() as tmp:
        result = dispatch_agent_message_graph(
            make_temp_config(Path(tmp)),
            {"session_id": "graph-route", "question": "Crawler please ignore this text and answer locally"},
            from_agent="User",
            content="This content names CrawlerAgent but is addressed to MCagent.",
            to_agent="MCagent",
            conversation_id="graph-route",
            agent_delivery=legacy,
        )
    assert_true("legacy_called_once", len(calls) == 1, str(calls))
    assert_true("routed_to_mcagent", calls[0].get("agent") == "mcagent_rag", str(calls[0]))
    runtime = result.get("graph_runtime") or {}
    assert_true("runtime_is_langgraph", runtime.get("runtime") == "langgraph", str(runtime))
    assert_true("active_agent_mcagent", runtime.get("active_agent") == "mcagent_rag", str(runtime))
    assert_true("mcagent_node_visited", "mcagent_graph.legacy_adapter" in runtime.get("visited_nodes", []), str(runtime))
    assert_true("crawler_node_not_visited", "crawler_graph.legacy_adapter" not in runtime.get("visited_nodes", []), str(runtime))
    agent_runtime = result.get("agent_graph_runtime") or {}
    assert_true("mcagent_subgraph", agent_runtime.get("agent_graph") == "MCagentGraph", str(agent_runtime))
    assert_true("mcagent_select_local_tools_node", "mcagent.select_local_tools" in agent_runtime.get("visited_nodes", []), str(agent_runtime))
    assert_true("mcagent_prepare_local_retrieval_node", "mcagent.prepare_local_retrieval" in agent_runtime.get("visited_nodes", []), str(agent_runtime))
    assert_true("mcagent_prepare_message_preflight_node", "mcagent.prepare_message_preflight_contract" in agent_runtime.get("visited_nodes", []), str(agent_runtime))
    assert_true("mcagent_prepare_contextual_question_node", "mcagent.prepare_contextual_question_contract" in agent_runtime.get("visited_nodes", []), str(agent_runtime))
    assert_true("mcagent_prepare_route_input_node", "mcagent.prepare_route_input_contract" in agent_runtime.get("visited_nodes", []), str(agent_runtime))
    assert_true("mcagent_prepare_runtime_request_node", "mcagent.prepare_runtime_request" in agent_runtime.get("visited_nodes", []), str(agent_runtime))
    assert_true("mcagent_legacy_adapter_node", "mcagent.legacy_adapter" in agent_runtime.get("visited_nodes", []), str(agent_runtime))
    assert_true("mcagent_prepare_route_result_node", "mcagent.prepare_route_result_contract" in agent_runtime.get("visited_nodes", []), str(agent_runtime))
    message_preflight = agent_runtime.get("message_preflight_contract") or {}
    assert_true("mcagent_message_preflight_exists", message_preflight.get("agent_id") == "mcagent_rag", str(message_preflight))
    assert_true("mcagent_message_preflight_context_false", (message_preflight.get("flags") or {}).get("context_only_agent_message") is False, str(message_preflight))
    assert_true("mcagent_message_preflight_collection_false", (message_preflight.get("flags") or {}).get("collection_request_agent_message") is False, str(message_preflight))
    assert_true("mcagent_message_preflight_no_tool", "tool" not in message_preflight and "route_intent" not in message_preflight, str(message_preflight))
    contextual_question = agent_runtime.get("contextual_question_contract") or {}
    assert_true("mcagent_contextual_question_kind", contextual_question.get("contract_kind") == "mcagent_contextual_question_contract", str(contextual_question))
    assert_true("mcagent_contextual_question_agent", contextual_question.get("agent_id") == "mcagent_rag", str(contextual_question))
    assert_true(
        "mcagent_contextual_question_original",
        contextual_question.get("original_question") == "This content names CrawlerAgent but is addressed to MCagent.",
        str(contextual_question),
    )
    assert_true(
        "mcagent_contextual_question_hint_unchanged",
        contextual_question.get("contextual_question_hint") == contextual_question.get("original_question"),
        str(contextual_question),
    )
    assert_true("mcagent_contextual_question_no_rewrite", contextual_question.get("rewrite_executed") is False, str(contextual_question))
    assert_true("mcagent_contextual_question_no_tool_decision", "tool" not in contextual_question and "route_intent" not in contextual_question, str(contextual_question))
    route_input = agent_runtime.get("route_input_contract") or {}
    assert_true("mcagent_route_input_kind", route_input.get("contract_kind") == "mcagent_route_input_contract", str(route_input))
    assert_true("mcagent_route_input_owner", route_input.get("decision_owner") == "MCagent LLM", str(route_input))
    assert_true("mcagent_route_input_has_rag_tool", "local_rag_search" in route_input.get("candidate_route_tools", []), str(route_input))
    assert_true("mcagent_route_input_no_tool_decision", "tool" not in route_input and "route_intent" not in route_input, str(route_input))
    assert_true("mcagent_route_input_links_message_preflight", route_input.get("message_preflight_contract_id") == message_preflight.get("contract_id"), str(route_input))
    assert_true("mcagent_route_input_links_contextual_question", route_input.get("contextual_question_contract_id") == contextual_question.get("contract_id"), str(route_input))
    runtime_request = agent_runtime.get("runtime_request") or {}
    assert_true("mcagent_runtime_request_kind", runtime_request.get("contract_kind") == "mcagent_local_runtime_request", str(runtime_request))
    assert_true("mcagent_runtime_request_owner", runtime_request.get("decision_owner") == "MCagent LLM", str(runtime_request))
    assert_true("mcagent_runtime_request_payload_agent", (runtime_request.get("payload") or {}).get("agent") == "mcagent_rag", str(runtime_request))
    assert_true("mcagent_runtime_request_links_message_preflight", runtime_request.get("message_preflight_contract_id") == message_preflight.get("contract_id"), str(runtime_request))
    assert_true("mcagent_runtime_request_links_contextual_question", runtime_request.get("contextual_question_contract_id") == contextual_question.get("contract_id"), str(runtime_request))
    assert_true("mcagent_runtime_request_links_route_input", runtime_request.get("route_input_contract_id") == route_input.get("contract_id"), str(runtime_request))
    adapter = agent_runtime.get("runtime_adapter") or {}
    assert_true("mcagent_runtime_adapter_visible", adapter.get("adapter") == "legacy_web_server_runtime", str(adapter))
    assert_true("mcagent_runtime_adapter_owner", adapter.get("decision_owner") == "MCagent LLM", str(adapter))
    assert_true("mcagent_adapter_consumed_runtime_request", adapter.get("runtime_request_id") == runtime_request.get("request_id"), str(adapter))
    assert_true("mcagent_adapter_links_message_preflight", adapter.get("message_preflight_contract_id") == message_preflight.get("contract_id"), str(adapter))
    assert_true("mcagent_adapter_links_contextual_question", adapter.get("contextual_question_contract_id") == contextual_question.get("contract_id"), str(adapter))
    assert_true("mcagent_adapter_links_route_input", adapter.get("route_input_contract_id") == route_input.get("contract_id"), str(adapter))
    assert_true("mcagent_adapter_contract_kind", adapter.get("contract_kind") == "mcagent_local_runtime_request", str(adapter))
    route_result = agent_runtime.get("route_result_contract") or {}
    result_shape = route_result.get("result_shape") or {}
    assert_true("mcagent_route_result_kind", route_result.get("contract_kind") == "mcagent_route_result_contract", str(route_result))
    assert_true("mcagent_route_result_owner", route_result.get("decision_owner") == "MCagent LLM", str(route_result))
    assert_true("mcagent_route_result_links_runtime_request", route_result.get("runtime_request_id") == runtime_request.get("request_id"), str(route_result))
    assert_true("mcagent_route_result_links_route_input", route_result.get("route_input_contract_id") == route_input.get("contract_id"), str(route_result))
    assert_true("mcagent_route_result_links_message_preflight", route_result.get("message_preflight_contract_id") == message_preflight.get("contract_id"), str(route_result))
    assert_true("mcagent_route_result_links_contextual_question", route_result.get("contextual_question_contract_id") == contextual_question.get("contract_id"), str(route_result))
    assert_true("mcagent_route_result_agent_shape", result_shape.get("agent") == "mcagent_rag", str(route_result))
    assert_true("mcagent_route_result_answer_shape", result_shape.get("answer_present") is True and result_shape.get("source_count") == 0, str(route_result))
    assert_true("mcagent_route_result_no_tool_decision", "tool" not in route_result and "route_intent" not in route_result and "action_plan" not in route_result, str(route_result))
    boundary = agent_runtime.get("tool_boundary") or {}
    assert_true("mcagent_local_only", "local_rag" in boundary.get("allowed_capability_groups", []), str(boundary))
    assert_true("mcagent_blocks_web", "web_search" in boundary.get("blocked_capability_groups", []), str(boundary))
    selected_groups = agent_runtime.get("selected_tool_groups") or {}
    local_tools = set(selected_groups.get("default_tools") or [])
    assert_true("mcagent_default_local_group", selected_groups.get("default_groups") == ["local"], str(selected_groups))
    assert_true("mcagent_local_tools_include_rag", "local_rag_search" in local_tools, str(selected_groups))
    assert_true("mcagent_local_tools_include_message_handoff", "delegate_crawler" in local_tools, str(selected_groups))
    assert_true("mcagent_local_tools_exclude_crawler_web", not {"web_discovery", "fetch_url", "playwright", "browser_collect", "modpack_download"} & local_tools, str(selected_groups))
    retrieval_contract = agent_runtime.get("retrieval_contract") or {}
    assert_true("mcagent_retrieval_local_sources", "local_rag" in retrieval_contract.get("allowed_evidence_sources", []), str(retrieval_contract))
    assert_true("mcagent_retrieval_blocks_public_web", "public_web" in retrieval_contract.get("blocked_evidence_sources", []), str(retrieval_contract))


def test_conversation_graph_can_dispatch_to_crawler_node() -> None:
    calls: list[dict[str, Any]] = []

    def legacy(config: AppConfig, payload: dict[str, Any], emit: Any | None = None) -> dict[str, Any]:  # noqa: ARG001
        calls.append(dict(payload))
        return {
            "answer": "crawler received",
            "agent": payload.get("agent"),
            "sources": [],
            "context": "",
        }

    with tempfile.TemporaryDirectory() as tmp:
        result = dispatch_agent_message_graph(
            make_temp_config(Path(tmp)),
            {"session_id": "graph-crawler"},
            from_agent="MCagent",
            content="Collect public data for this target.",
            to_agent="CrawlerAgent",
            intent="collection_request",
            conversation_id="graph-crawler",
            metadata={"delivery_target": "MCagent/RAG"},
            agent_delivery=legacy,
        )
    assert_true("legacy_called_once", len(calls) == 1, str(calls))
    assert_true("routed_to_crawler", calls[0].get("agent") == "crawler_agent", str(calls[0]))
    message = calls[0].get("agent_message") or {}
    assert_true("message_preserved", message.get("from_agent") == "MCagent" and message.get("to_agent") == "CrawlerAgent", str(message))
    runtime = result.get("graph_runtime") or {}
    assert_true("crawler_node_visited", "crawler_graph.legacy_adapter" in runtime.get("visited_nodes", []), str(runtime))
    agent_runtime = result.get("agent_graph_runtime") or {}
    assert_true("crawler_subgraph", agent_runtime.get("agent_graph") == "CrawlerAgentGraph", str(agent_runtime))
    assert_true("crawler_select_tool_groups_node", "crawler.select_tool_groups" in agent_runtime.get("visited_nodes", []), str(agent_runtime))
    assert_true("crawler_prepare_mission_contract_node", "crawler.prepare_mission_contract" in agent_runtime.get("visited_nodes", []), str(agent_runtime))
    assert_true("crawler_prepare_source_planning_node", "crawler.prepare_source_planning_contract" in agent_runtime.get("visited_nodes", []), str(agent_runtime))
    assert_true("crawler_prepare_message_preflight_node", "crawler.prepare_message_preflight_contract" in agent_runtime.get("visited_nodes", []), str(agent_runtime))
    assert_true("crawler_prepare_route_input_node", "crawler.prepare_route_input_contract" in agent_runtime.get("visited_nodes", []), str(agent_runtime))
    assert_true("crawler_prepare_runtime_request_node", "crawler.prepare_runtime_request" in agent_runtime.get("visited_nodes", []), str(agent_runtime))
    assert_true("crawler_legacy_adapter_node", "crawler.legacy_adapter" in agent_runtime.get("visited_nodes", []), str(agent_runtime))
    assert_true("crawler_prepare_route_result_node", "crawler.prepare_route_result_contract" in agent_runtime.get("visited_nodes", []), str(agent_runtime))
    source_planning = agent_runtime.get("source_planning_contract") or {}
    assert_true("crawler_source_planning_kind", source_planning.get("contract_kind") == "crawler_source_planning_input_contract", str(source_planning))
    assert_true("crawler_source_planning_owner", source_planning.get("decision_owner") == "CrawlerAgent LLM", str(source_planning))
    assert_true("crawler_source_planning_question", source_planning.get("planning_question") == "Collect public data for this target.", str(source_planning))
    assert_true("crawler_source_planning_delivery", source_planning.get("delivery_target") == "MCagent/RAG", str(source_planning))
    assert_true("crawler_source_planning_has_general_tools", "fetch_url" in source_planning.get("candidate_general_tools", []), str(source_planning))
    assert_true("crawler_source_planning_domain_candidates", "minecraft" in (source_planning.get("candidate_domain_toolsets") or {}), str(source_planning))
    assert_true("crawler_source_planning_no_plan_output", not {"tool", "route_intent", "sources", "tasks", "action_plan", "selected_sources"} & set(source_planning), str(source_planning))
    message_preflight = agent_runtime.get("message_preflight_contract") or {}
    assert_true("crawler_message_preflight_exists", message_preflight.get("agent_id") == "crawler_agent", str(message_preflight))
    assert_true("crawler_message_preflight_collection", (message_preflight.get("flags") or {}).get("collection_request_agent_message") is True, str(message_preflight))
    assert_true("crawler_message_preflight_no_tool", "tool" not in message_preflight and "route_intent" not in message_preflight, str(message_preflight))
    route_input = agent_runtime.get("route_input_contract") or {}
    assert_true("crawler_route_input_kind", route_input.get("contract_kind") == "crawler_route_input_contract", str(route_input))
    assert_true("crawler_route_input_owner", route_input.get("decision_owner") == "CrawlerAgent LLM", str(route_input))
    assert_true("crawler_route_input_has_fetch_tool", "fetch_url" in route_input.get("candidate_route_tools", []), str(route_input))
    assert_true("crawler_route_input_no_tool_decision", "tool" not in route_input and "route_intent" not in route_input, str(route_input))
    assert_true("crawler_route_input_links_message_preflight", route_input.get("message_preflight_contract_id") == message_preflight.get("contract_id"), str(route_input))
    assert_true("crawler_route_input_links_source_planning", route_input.get("source_planning_contract_id") == source_planning.get("contract_id"), str(route_input))
    runtime_request = agent_runtime.get("runtime_request") or {}
    assert_true("crawler_runtime_request_kind", runtime_request.get("contract_kind") == "crawler_collection_runtime_request", str(runtime_request))
    assert_true("crawler_runtime_request_owner", runtime_request.get("decision_owner") == "CrawlerAgent LLM", str(runtime_request))
    assert_true("crawler_runtime_request_payload_agent", (runtime_request.get("payload") or {}).get("agent") == "crawler_agent", str(runtime_request))
    assert_true("crawler_runtime_request_links_message_preflight", runtime_request.get("message_preflight_contract_id") == message_preflight.get("contract_id"), str(runtime_request))
    assert_true("crawler_runtime_request_links_source_planning", runtime_request.get("source_planning_contract_id") == source_planning.get("contract_id"), str(runtime_request))
    assert_true("crawler_runtime_request_links_route_input", runtime_request.get("route_input_contract_id") == route_input.get("contract_id"), str(runtime_request))
    request_message = runtime_request.get("message") or {}
    assert_true("crawler_runtime_request_delivery", request_message.get("delivery_target") == "MCagent/RAG", str(runtime_request))
    adapter = agent_runtime.get("runtime_adapter") or {}
    assert_true("crawler_runtime_adapter_visible", adapter.get("adapter") == "legacy_web_server_runtime", str(adapter))
    assert_true("crawler_runtime_adapter_owner", adapter.get("decision_owner") == "CrawlerAgent LLM", str(adapter))
    assert_true("crawler_adapter_consumed_runtime_request", adapter.get("runtime_request_id") == runtime_request.get("request_id"), str(adapter))
    assert_true("crawler_adapter_links_message_preflight", adapter.get("message_preflight_contract_id") == message_preflight.get("contract_id"), str(adapter))
    assert_true("crawler_adapter_links_source_planning", adapter.get("source_planning_contract_id") == source_planning.get("contract_id"), str(adapter))
    assert_true("crawler_adapter_links_route_input", adapter.get("route_input_contract_id") == route_input.get("contract_id"), str(adapter))
    assert_true("crawler_adapter_contract_kind", adapter.get("contract_kind") == "crawler_collection_runtime_request", str(adapter))
    route_result = agent_runtime.get("route_result_contract") or {}
    result_shape = route_result.get("result_shape") or {}
    assert_true("crawler_route_result_kind", route_result.get("contract_kind") == "crawler_route_result_contract", str(route_result))
    assert_true("crawler_route_result_owner", route_result.get("decision_owner") == "CrawlerAgent LLM", str(route_result))
    assert_true("crawler_route_result_links_runtime_request", route_result.get("runtime_request_id") == runtime_request.get("request_id"), str(route_result))
    assert_true("crawler_route_result_links_route_input", route_result.get("route_input_contract_id") == route_input.get("contract_id"), str(route_result))
    assert_true("crawler_route_result_links_message_preflight", route_result.get("message_preflight_contract_id") == message_preflight.get("contract_id"), str(route_result))
    assert_true("crawler_route_result_links_source_planning", route_result.get("source_planning_contract_id") == source_planning.get("contract_id"), str(route_result))
    assert_true("crawler_route_result_agent_shape", result_shape.get("agent") == "crawler_agent", str(route_result))
    assert_true("crawler_route_result_answer_shape", result_shape.get("answer_present") is True and result_shape.get("source_count") == 0, str(route_result))
    assert_true("crawler_route_result_no_tool_decision", "tool" not in route_result and "route_intent" not in route_result and "action_plan" not in route_result, str(route_result))
    boundary = agent_runtime.get("tool_boundary") or {}
    assert_true("crawler_general_web", "web_discovery" in boundary.get("allowed_capability_groups", []), str(boundary))
    assert_true("crawler_optional_domain_toolsets", "optional_domain_toolsets" in boundary.get("allowed_capability_groups", []), str(boundary))
    general_tools = set(boundary.get("general_collection_tools") or [])
    minecraft_tools = set((boundary.get("domain_toolsets") or {}).get("minecraft") or [])
    assert_true("crawler_general_tools_include_fetch", {"web_discovery", "fetch_url", "playwright"}.issubset(general_tools), str(boundary))
    assert_true("crawler_general_tools_exclude_minecraft", not {"mcmod", "modrinth", "modpack_download", "modpack_internal"} & general_tools, str(boundary))
    assert_true("crawler_minecraft_domain_tools", {"mcmod", "modrinth", "modpack_download", "modpack_internal"}.issubset(minecraft_tools), str(boundary))
    selected_groups = agent_runtime.get("selected_tool_groups") or {}
    assert_true("crawler_default_general_only", selected_groups.get("default_groups") == ["general"], str(selected_groups))
    assert_true("crawler_domain_candidates_visible", "minecraft" in (selected_groups.get("candidate_domain_toolsets") or {}), str(selected_groups))
    assert_true("crawler_selection_owned_by_llm", selected_groups.get("decision_owner") == "CrawlerAgent LLM", str(selected_groups))
    mission_contract = agent_runtime.get("mission_contract") or {}
    assert_true("crawler_mission_delivery", mission_contract.get("delivery_target") == "MCagent/RAG", str(mission_contract))
    assert_true("crawler_mission_owner", mission_contract.get("decision_owner") == "CrawlerAgent LLM", str(mission_contract))


def test_non_streaming_graph_reuses_checkpointed_runtime_without_reusing_emit() -> None:
    calls: list[str] = []
    emitted: list[tuple[str, Any]] = []

    def legacy(config: AppConfig, payload: dict[str, Any], emit: Any | None = None) -> dict[str, Any]:  # noqa: ARG001
        calls.append(str(payload.get("session_id") or ""))
        if emit is not None:
            emit("legacy", {"session_id": payload.get("session_id")})
        return {"answer": "ok", "agent": payload.get("agent"), "sources": [], "context": ""}

    graph_runtime_module._GRAPH_CACHE.clear()
    with tempfile.TemporaryDirectory() as tmp:
        config = make_temp_config(Path(tmp))
        dispatch_agent_message_graph(
            config,
            {"session_id": "cache-a"},
            from_agent="User",
            content="hello",
            to_agent="MCagent",
            conversation_id="cache-a",
            agent_delivery=legacy,
        )
        dispatch_agent_message_graph(
            config,
            {"session_id": "cache-b"},
            from_agent="User",
            content="hello again",
            to_agent="MCagent",
            conversation_id="cache-b",
            agent_delivery=legacy,
        )
        assert_true("non_streaming_cache_one_graph", len(graph_runtime_module._GRAPH_CACHE) == 1, str(graph_runtime_module._GRAPH_CACHE))
        dispatch_agent_message_graph(
            config,
            {"session_id": "stream-c"},
            from_agent="User",
            content="stream",
            to_agent="MCagent",
            conversation_id="stream-c",
            agent_delivery=legacy,
            emit=lambda event, data: emitted.append((event, data)),
        )
    assert_true("all_calls_delivered", calls == ["cache-a", "cache-b", "stream-c"], str(calls))
    assert_true("stream_emit_observed", any(event == "legacy" for event, _data in emitted), str(emitted))
    assert_true("stream_graph_not_cached", len(graph_runtime_module._GRAPH_CACHE) == 1, str(graph_runtime_module._GRAPH_CACHE))


def test_agent_subgraphs_load_session_memory_context() -> None:
    session_id = "graph-memory-context"
    DEFAULT_SESSION_STORE.delete(session_id)
    DEFAULT_SESSION_STORE.append_turn(session_id, {"question": "first question", "answer": "first answer"})

    def legacy(config: AppConfig, payload: dict[str, Any], emit: Any | None = None) -> dict[str, Any]:  # noqa: ARG001
        return {"answer": "ok", "agent": payload.get("agent"), "sources": [], "context": ""}

    with tempfile.TemporaryDirectory() as tmp:
        mc_result = dispatch_agent_message_graph(
            make_temp_config(Path(tmp)),
            {"session_id": session_id},
            from_agent="User",
            content="use memory",
            to_agent="MCagent",
            conversation_id=session_id,
            agent_delivery=legacy,
        )
        crawler_result = dispatch_agent_message_graph(
            make_temp_config(Path(tmp)),
            {"session_id": session_id},
            from_agent="User",
            content="use memory too",
            to_agent="CrawlerAgent",
            conversation_id=session_id,
            agent_delivery=legacy,
        )
    mc_memory = (mc_result.get("agent_graph_runtime") or {}).get("memory_context") or {}
    mc_contextual_question = (mc_result.get("agent_graph_runtime") or {}).get("contextual_question_contract") or {}
    crawler_memory = (crawler_result.get("agent_graph_runtime") or {}).get("memory_context") or {}
    assert_true("mcagent_memory_session", mc_memory.get("session_id") == session_id, str(mc_memory))
    assert_true("mcagent_memory_turn_count", mc_memory.get("turn_count") == 1, str(mc_memory))
    assert_true("mcagent_contextual_question_history_count", mc_contextual_question.get("history_turn_count") == 1, str(mc_contextual_question))
    assert_true("mcagent_contextual_question_recent_history", "first question" in mc_contextual_question.get("recent_questions", []), str(mc_contextual_question))
    assert_true("crawler_memory_session", crawler_memory.get("session_id") == session_id, str(crawler_memory))
    assert_true("crawler_memory_turn_count", crawler_memory.get("turn_count") == 1, str(crawler_memory))
    DEFAULT_SESSION_STORE.delete(session_id)


def test_crawler_background_job_enters_langgraph_runtime() -> None:
    class FakeJob:
        id = "job-graph-test"
        result: dict[str, Any] | None = None

    calls: list[dict[str, Any]] = []

    def agent_loop(job: FakeJob, payload: dict[str, Any], config: AppConfig) -> None:  # noqa: ARG001
        calls.append(dict(payload))
        job.result = {"agent_loop": "ran"}

    with tempfile.TemporaryDirectory() as tmp:
        job = FakeJob()
        run_crawler_job_graph(
            make_temp_config(Path(tmp)),
            job,
            {"session_id": "crawler-job-graph", "source": "planner", "delivery_target": "MCagent/RAG", "agent_message": {"ok": True}},
            agent_loop=agent_loop,
        )
    assert_true("agent_loop_called", len(calls) == 1, str(calls))
    runtime = (job.result or {}).get("crawler_job_graph_runtime") or {}
    assert_true("job_graph_runtime", runtime.get("graph") == "CrawlerJobGraph", str(runtime))
    assert_true("job_graph_receive", "crawler_job.receive" in runtime.get("visited_nodes", []), str(runtime))
    assert_true("job_graph_agent_loop", "crawler_job.agent_loop" in runtime.get("visited_nodes", []), str(runtime))
    contract = runtime.get("job_contract") or {}
    assert_true("job_graph_contract_delivery", contract.get("delivery_target") == "MCagent/RAG", str(contract))
    assert_true("job_graph_contract_message", contract.get("has_agent_message") is True, str(contract))
    assert_true("job_graph_contract_owner", contract.get("decision_owner") == "CrawlerAgent LLM", str(contract))


def test_crawler_job_plan_preparation_is_objective_and_reusable() -> None:
    class FakeJob:
        id = "prepare-plan"
        result: dict[str, Any] | None = {"reuse_signature": "reuse-1", "requested_by": "unit"}
        stop_requested = False
        ended_at = None

    original_plan = web_server._plan_crawler_with_job_timeout
    try:
        web_server._plan_crawler_with_job_timeout = lambda *_args, **_kwargs: {  # type: ignore[assignment]
            "topic": "unit topic",
            "tasks": [{"source": "web_discovery", "query": "unit query"}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            job = FakeJob()
            prepared = web_server._prepare_crawler_job_plan(
                job=job,
                payload={"source": "planner", "session_summary": {"delivery_target": "human"}},
                config=make_temp_config(Path(tmp)),
                source="planner",
                question="unit question",
                job_setup=web_server.CrawlerJobSetupService(),
                job_progress=web_server.CrawlerJobProgressService(),
            )
    finally:
        web_server._plan_crawler_with_job_timeout = original_plan  # type: ignore[assignment]
    assert_true("prepared_not_stopped", prepared.get("stopped") is False, str(prepared))
    assert_true("prepared_tasks", prepared.get("tasks") == [{"source": "web_discovery", "query": "unit query"}], str(prepared))
    assert_true("prepared_session_summary", prepared.get("session_summary") == {"delivery_target": "human"}, str(prepared))
    assert_true("job_planned_result_keeps_reuse", (job.result or {}).get("reuse_signature") == "reuse-1", str(job.result))

    single = web_server._prepare_crawler_job_plan(
        job=FakeJob(),
        payload={"source": "fetch_url", "query": "https://example.com"},
        config=make_temp_config(Path(tempfile.gettempdir())),
        source="fetch_url",
        question="fallback question",
        job_setup=web_server.CrawlerJobSetupService(),
        job_progress=web_server.CrawlerJobProgressService(),
    )
    assert_true("single_source_tasks", single.get("tasks") == [{"source": "fetch_url", "query": "https://example.com", "reason": "single source request"}], str(single))


def test_crawler_task_preparation_routes_archive_urls_objectively() -> None:
    plan: dict[str, Any] = {"topic": "archive test"}
    prepared = web_server._prepare_crawler_task_execution(
        payload={"session_id": "task-prep"},
        task={"source": "fetch_url", "query": "https://example.com/demo.mrpack", "reason": "download archive"},
        question="download archive",
        plan=plan,
        current_index=1,
        artifact_refs=web_server.ArtifactReferenceService(),
        task_preparation=web_server.CrawlerTaskPreparationService(),
    )
    assert_true("archive_routed_to_download", prepared.get("task_source") == "modpack_download", str(prepared))
    assert_true("archive_payload_source", prepared.get("task_payload", {}).get("source") == "modpack_download", str(prepared))
    reflections = plan.get("agent_reflections") or []
    assert_true("archive_reflection_recorded", any(item.get("action") == "route_archive_url_to_modpack_download" for item in reflections), str(reflections))


def test_crawler_task_result_metadata_is_recorded_objectively() -> None:
    result = {"returncode": 1, "output": "network error", "export_dir": ""}
    plan: dict[str, Any] = {}
    records = web_server._record_crawler_task_result_metadata(
        result=result,
        task={"reason": "unit reason"},
        task_source="fetch_url",
        task_payload={"query": "https://example.com/missing"},
        question="unit question",
        plan=plan,
        result_index=1,
        artifact_refs=web_server.ArtifactReferenceService(),
    )
    assert_true("metadata_records_zero", records == 0, str(result))
    assert_true("metadata_query", result.get("query") == "https://example.com/missing", str(result))
    assert_true("metadata_reason", result.get("reason") == "unit reason", str(result))
    stats = result.get("manifest_stats") or {}
    assert_true("metadata_inline_failure_stats", stats == {"records": 0, "skipped": 0, "errors": 0}, str(result))


def test_crawler_task_accounting_inserts_archive_internal_followup() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        export_dir = str(Path(tmp) / "downloaded_archive")
        result = {
            "returncode": 0,
            "manifest_stats": {"records": 1, "downloads": 1},
            "export_dir": export_dir,
        }
        plan: dict[str, Any] = {"delivery_target": "MCagent/RAG"}
        tasks = [{"source": "modpack_download", "query": "public archive"}]
        update = web_server._apply_crawler_task_accounting(
            result=result,
            task_source="modpack_download",
            task_payload={"query": "public archive"},
            question="public archive",
            payload={},
            plan=plan,
            tasks=tasks,
            index=1,
            max_total_tasks=4,
            result_accounting=web_server.CrawlerResultAccountingService(),
        )
    assert_true("accounting_success", update.get("success_delta") == 1, str(update))
    assert_true("accounting_needs_ingest", update.get("needs_ingest") is True, str(update))
    assert_true("accounting_export_dir", update.get("accepted_export_dirs") == [export_dir], str(update))
    assert_true("accounting_followup_inserted", update.get("inserted_followup") is True, str(update))
    assert_true("internal_followup_source", tasks[1].get("source") == "modpack_internal", str(tasks))
    assert_true("objective_reflection_recorded", any(item.get("action") == "add_tasks" for item in plan.get("agent_reflections") or []), str(plan))


def test_crawler_task_accounting_does_not_duplicate_followup() -> None:
    result = {
        "returncode": 0,
        "manifest_stats": {"records": 1, "downloads": 1},
        "export_dir": "",
    }
    plan: dict[str, Any] = {"delivery_target": "MCagent/RAG"}
    tasks = [
        {"source": "modpack_download", "query": "public archive"},
        {"source": "modpack_internal", "query": "public archive"},
    ]
    update = web_server._apply_crawler_task_accounting(
        result=result,
        task_source="modpack_download",
        task_payload={"query": "public archive"},
        question="public archive",
        payload={},
        plan=plan,
        tasks=tasks,
        index=1,
        max_total_tasks=4,
        result_accounting=web_server.CrawlerResultAccountingService(),
    )
    assert_true("duplicate_followup_not_inserted", update.get("inserted_followup") is False, str(update))
    assert_true("tasks_unchanged", len(tasks) == 2, str(tasks))


def test_mcagent_context_followup_extends_budget_for_external_collection() -> None:
    result = {
        "returncode": 0,
        "manifest_stats": {"records": 1},
        "export_dir": "",
    }
    plan: dict[str, Any] = {"delivery_target": "MCagent/RAG"}
    tasks = [
        {"source": "mcagent_context", "query": "乌托邦缺口"},
        {"source": "web_discovery", "query": "existing broad search"},
    ]
    update = web_server._apply_crawler_task_accounting(
        result=result,
        task_source="mcagent_context",
        task_payload={"query": "乌托邦缺口"},
        question="乌托邦缺口",
        payload={},
        plan=plan,
        tasks=tasks,
        index=1,
        max_total_tasks=2,
        result_accounting=web_server.CrawlerResultAccountingService(),
    )
    assert_true("context_followup_inserted", update.get("inserted_followup") is True, str(update))
    assert_true("followup_source", tasks[1].get("source") == "web_discovery", str(tasks))
    assert_true("budget_extended", plan.get("max_total_tasks_extended_for_context_followup") == 3, str(plan))
    reflections = plan.get("agent_reflections") or []
    assert_true("reflection_reason_specific", "MCagent/RAG gap analysis is available" in str(reflections[-1].get("reason") or ""), str(reflections))


def test_crawler_loop_executes_materialized_tasks_before_reflecting_again() -> None:
    source = Path(web_server.__file__).read_text(encoding="utf-8")
    assert_true("skip_reflection_marker", "skip_reflection_once_at_index" in source)
    assert_true("inserted_task_sets_skip", 'if reflection_update.get("inserted_tasks")' in source)
    assert_true("execute_materialized_trace", "execute_materialized_task" in source)


def test_crawler_task_accounting_turns_archive_fetch_observation_into_download_followup() -> None:
    result = {
        "returncode": 1,
        "manifest_stats": {
            "records": 0,
            "skipped": 1,
            "archive_url_detected": True,
            "failure_reason": "URL points to a binary modpack archive.",
        },
        "export_dir": "",
    }
    plan: dict[str, Any] = {"delivery_target": "MCagent/RAG"}
    tasks = [{"source": "fetch_url", "query": "https://example.com/demo.mrpack"}]
    update = web_server._apply_crawler_task_accounting(
        result=result,
        task_source="fetch_url",
        task_payload={"query": "https://example.com/demo.mrpack"},
        question="download demo",
        payload={},
        plan=plan,
        tasks=tasks,
        index=1,
        max_total_tasks=4,
        result_accounting=web_server.CrawlerResultAccountingService(),
    )
    assert_true("archive_fetch_candidate", update.get("candidate_delta") == 1, str(update))
    assert_true("archive_fetch_not_failure", update.get("failure_delta") == 0, str(update))
    assert_true("download_followup_inserted", update.get("inserted_followup") is True, str(update))
    assert_true("download_followup_source", tasks[1].get("source") == "modpack_download", str(tasks))


def test_crawler_task_step_blocks_empty_query_before_tool_execution() -> None:
    class FakeJob:
        id = "empty-query-step"
        result: dict[str, Any] | None = None
        status = "queued"
        summary = ""
        error = None
        created_at = 0.0
        started_at = None
        ended_at = None
        stop_requested = False

    with tempfile.TemporaryDirectory() as tmp:
        plan: dict[str, Any] = {}
        tasks = [{"source": "web_discovery", "query": "", "reason": "missing query"}]
        task_results: list[dict[str, Any]] = []
        step = web_server._execute_crawler_task_step(
            job=FakeJob(),
            config=make_temp_config(Path(tmp)),
            payload={},
            task=tasks[0],
            question="",
            plan=plan,
            tasks=tasks,
            index=1,
            task_results=task_results,
            session_summary=None,
            artifact_refs=web_server.ArtifactReferenceService(),
            task_preparation=web_server.CrawlerTaskPreparationService(),
            result_accounting=web_server.CrawlerResultAccountingService(),
            job_progress=web_server.CrawlerJobProgressService(),
            max_total_tasks=4,
        )
    assert_true("empty_query_continue", step.get("continue_loop") is True, str(step))
    assert_true("empty_query_failure", step.get("failure_delta") == 1, str(step))
    assert_true("empty_query_bad_streak", step.get("bad_streak_delta") == 1, str(step))
    assert_true("empty_query_result_recorded", len(task_results) == 1, str(task_results))
    assert_true("empty_query_no_accounting_success", not step.get("success_delta"), str(step))


def test_crawler_task_step_executes_command_and_records_accounting() -> None:
    class FakeJob:
        id = "command-step"
        result: dict[str, Any] | None = None
        status = "queued"
        summary = ""
        error = None
        created_at = 0.0
        started_at = None
        ended_at = None
        stop_requested = False

    original_command = web_server._run_crawler_command
    try:
        export_dir_holder: dict[str, str] = {}

        def fake_command(_command, _source, job=None):  # noqa: ANN001, ARG001
            export_dir = export_dir_holder["path"]
            return {
                "returncode": 0,
                "output": "collected",
                "export_dir": export_dir,
                "topic_validation": {"matched": True},
            }

        web_server._run_crawler_command = fake_command  # type: ignore[assignment]
        with tempfile.TemporaryDirectory() as tmp:
            export_dir = Path(tmp) / "export"
            export_dir.mkdir()
            (export_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "records": [{"title": "Downloaded Archive", "url": "https://example.com/unit.mrpack", "text": "download evidence"}],
                        "downloads": [{"url": "https://example.com/unit.mrpack", "path": str(export_dir / "unit.mrpack")}],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            export_dir_holder["path"] = str(export_dir)
            plan: dict[str, Any] = {"delivery_target": "MCagent/RAG"}
            tasks = [{"source": "modpack_download", "query": "https://example.com/unit.mrpack", "reason": "download"}]
            task_results: list[dict[str, Any]] = []
            step = web_server._execute_crawler_task_step(
                job=FakeJob(),
                config=make_temp_config(Path(tmp)),
                payload={},
                task=tasks[0],
                question="download unit archive",
                plan=plan,
                tasks=tasks,
                index=1,
                task_results=task_results,
                session_summary=None,
                artifact_refs=web_server.ArtifactReferenceService(),
                task_preparation=web_server.CrawlerTaskPreparationService(),
                result_accounting=web_server.CrawlerResultAccountingService(),
                job_progress=web_server.CrawlerJobProgressService(),
                max_total_tasks=4,
            )
    finally:
        web_server._run_crawler_command = original_command  # type: ignore[assignment]
    assert_true("step_not_blocked", step.get("continue_loop") is False, str(step))
    assert_true("step_success", step.get("success_delta") == 1, str(step))
    assert_true("step_result_recorded", len(task_results) == 1, str(task_results))
    assert_true("step_observation_recorded", isinstance(task_results[0].get("observation"), dict), str(task_results))
    assert_true("step_query_recorded", task_results[0].get("query") == "https://example.com/unit.mrpack", str(task_results))


def test_crawler_task_step_ignores_unbacked_tool_record_claims() -> None:
    class FakeJob:
        id = "unbacked-command-step"
        result: dict[str, Any] | None = None
        status = "queued"
        summary = ""
        error = None
        created_at = 0.0
        started_at = None
        ended_at = None
        stop_requested = False

    original_command = web_server._run_crawler_command
    try:
        web_server._run_crawler_command = lambda _command, _source, job=None: {  # type: ignore[assignment]
            "returncode": 0,
            "output": "collected",
            "export_dir": "",
            "manifest_stats": {"records": 1},
            "topic_validation": {"matched": True},
        }
        with tempfile.TemporaryDirectory() as tmp:
            plan: dict[str, Any] = {"delivery_target": "MCagent/RAG"}
            tasks = [{"source": "web_discovery", "query": "unit query", "reason": "collect"}]
            task_results: list[dict[str, Any]] = []
            step = web_server._execute_crawler_task_step(
                job=FakeJob(),
                config=make_temp_config(Path(tmp)),
                payload={},
                task=tasks[0],
                question="unit query",
                plan=plan,
                tasks=tasks,
                index=1,
                task_results=task_results,
                session_summary=None,
                artifact_refs=web_server.ArtifactReferenceService(),
                task_preparation=web_server.CrawlerTaskPreparationService(),
                result_accounting=web_server.CrawlerResultAccountingService(),
                job_progress=web_server.CrawlerJobProgressService(),
                max_total_tasks=4,
            )
    finally:
        web_server._run_crawler_command = original_command  # type: ignore[assignment]
    assert_true("step_not_blocked", step.get("continue_loop") is False, str(step))
    assert_true("unbacked_claim_not_success", step.get("success_delta") == 0, str(step))
    assert_true("unbacked_claim_failure", step.get("failure_delta") == 1, str(step))
    assert_true("step_result_recorded", len(task_results) == 1, str(task_results))
    assert_true("step_observation_recorded", isinstance(task_results[0].get("observation"), dict), str(task_results))
    assert_true("step_query_recorded", task_results[0].get("query") == "unit query", str(task_results))


def test_crawler_loop_control_finishes_after_rag_success_checkpoint() -> None:
    class FakeJob:
        id = "loop-finish"
        result: dict[str, Any] | None = None
        status = "queued"
        summary = ""
        error = None
        created_at = 0.0
        started_at = None
        ended_at = None
        stop_requested = False

    with tempfile.TemporaryDirectory() as tmp:
        plan: dict[str, Any] = {"delivery_target": "MCagent/RAG"}
        task_results = [
            {
                "source": "web_discovery",
                "returncode": 0,
                "manifest_stats": {"records": 1, "usable_records": 1, "empty_records": 0},
                "topic_validation": {"matched": True},
            }
            for _index in range(4)
        ]
        loop_update = web_server._apply_crawler_loop_control_after_task(
            job=FakeJob(),
            config=make_temp_config(Path(tmp)),
            payload={},
            source="planner",
            question="unit question",
            plan=plan,
            tasks=[{"source": "web_discovery", "query": "unit question"}],
            task_results=task_results,
            index=1,
            success_count=1,
            candidate_count=0,
            bad_streak=0,
            replan_count=0,
            max_replans=2,
            max_total_tasks=4,
            loop_control=web_server.CrawlerLoopControlService(),
            job_progress=web_server.CrawlerJobProgressService(),
        )
    assert_true("loop_finish_action", loop_update.get("action") == "finish", str(loop_update))
    assert_true("loop_finish_reason", "usable evidence" in str(plan.get("agent_finish_reason") or ""), str(plan))
    assert_true("loop_finish_reflection", any(item.get("action") == "finish" for item in plan.get("agent_reflections") or []), str(plan))


def test_crawler_loop_does_not_finish_when_guide_coverage_unmet() -> None:
    class FakeJob:
        id = "loop-guide-unmet"
        result: dict[str, Any] | None = None
        status = "queued"
        summary = ""
        error = None
        created_at = 0.0
        started_at = None
        ended_at = None
        stop_requested = False

    with tempfile.TemporaryDirectory() as tmp:
        plan: dict[str, Any] = {
            "delivery_target": "MCagent/RAG",
            "coverage_goals": ["项目介绍", "版本/下载页线索", "玩法入门和可靠来源"],
        }
        task_results = [
            {
                "source": "fetch_url",
                "query": "https://modrinth.com/mod/farmers-delight",
                "returncode": 0,
                "manifest_stats": {"records": 1, "usable_records": 1, "empty_records": 0},
                "observation": {"status": "ok"},
                "topic_validation": {"matched": True, "reason": "direct", "notes": "Project page with description and downloads."},
            }
            for _index in range(4)
        ]
        loop_update = web_server._apply_crawler_loop_control_after_task(
            job=FakeJob(),
            config=make_temp_config(Path(tmp)),
            payload={},
            source="planner",
            question="Farmer's Delight guide coverage",
            plan=plan,
            tasks=[{"source": "web_discovery", "query": "Farmer's Delight guide"}],
            task_results=task_results,
            index=1,
            success_count=1,
            candidate_count=0,
            bad_streak=0,
            replan_count=0,
            max_replans=2,
            max_total_tasks=8,
            loop_control=web_server.CrawlerLoopControlService(),
            job_progress=web_server.CrawlerJobProgressService(),
        )
    assert_true("loop_continues_for_unmet_guide", loop_update.get("action") != "finish", str(loop_update))
    assert_true("no_finish_reflection", not any(item.get("action") == "finish" for item in plan.get("agent_reflections") or []), str(plan))


def test_crawler_reflection_helper_returns_objective_contract_feedback() -> None:
    original_reflect = web_server._reflect_crawler_progress_with_timeout

    def fake_reflect(*_args, **_kwargs):  # noqa: ANN001
        return {
            "action": "add_tasks",
            "reason": "try internal parsing before archive exists",
            "planner": "fake-crawler-llm",
            "tasks": [{"source": "modpack_internal", "query": "parse this pack"}],
            "contract": {"valid": True, "issues": [], "requires_llm_task_materialization": False},
        }

    class FakeJob:
        id = "reflection-contract-job"
        result = {}
        stop_requested = False

    with tempfile.TemporaryDirectory() as tmp:
        plan: dict[str, Any] = {"topic": "unit topic"}
        tasks = [{"source": "web_discovery", "query": "unit topic"}]
        task_results: list[dict[str, Any]] = []
        web_server._reflect_crawler_progress_with_timeout = fake_reflect  # type: ignore[assignment]
        try:
            result = web_server._apply_crawler_reflection_before_task(
                job=FakeJob(),
                config=make_temp_config(Path(tmp)),
                question="unit topic",
                plan=plan,
                tasks=tasks,
                task_results=task_results,
                index=0,
                session_summary={},
                max_total_tasks=4,
                runtime_step=web_server.CrawlerRuntimeStepService(),
                task_materializer=web_server.CrawlerTaskMaterializationService(),
                job_progress=web_server.CrawlerJobProgressService(),
            )
        finally:
            web_server._reflect_crawler_progress_with_timeout = original_reflect  # type: ignore[assignment]
    assert_true("contract_feedback_can_continue", result.get("continue_loop") in {True, False}, str(result))
    assert_true("blocked_feedback_recorded", any(item.get("source") == "crawler_reflection_contract" for item in task_results), str(task_results))
    blocked = next(item for item in task_results if item.get("source") == "crawler_reflection_contract")
    assert_true("objective_output", "no tool judged content relevance" in str(blocked.get("output") or ""), str(blocked))
    assert_true("contract_issue_visible", "requires_any" in str(blocked.get("capability_preflight") or ""), str(blocked))
    assert_true("reflection_not_acceptance", not blocked.get("accepted_sources") and not blocked.get("records"), str(blocked))
    assert_true("plan_records_contract", any(item.get("action") == "blocked_unexecutable_tasks" for item in plan.get("agent_reflections") or []), str(plan))


def test_crawler_after_task_review_prunes_duplicate_mcagent_context() -> None:
    class FakeJob:
        id = "after-task-review-job"
        result = {}
        stop_requested = False

    with tempfile.TemporaryDirectory() as tmp:
        plan: dict[str, Any] = {}
        tasks = [
            {"source": "mcagent_context", "query": "first local context"},
            {"source": "mcagent_context", "query": "duplicate local context"},
            {"source": "web_discovery", "query": "external followup"},
        ]
        task_results = [{"source": "mcagent_context", "returncode": 0, "manifest_stats": {"records": 1}}]
        result = web_server._apply_crawler_after_task_review(
            job=FakeJob(),
            config=make_temp_config(Path(tmp)),
            question="unit topic",
            plan=plan,
            tasks=tasks,
            task_results=task_results,
            index=1,
            task_source="mcagent_context",
            result={"returncode": 0},
            records_loaded=1,
            max_total_tasks=4,
            topic_discovery_review=web_server.CrawlerTopicDiscoveryReviewService(),
            job_progress=web_server.CrawlerJobProgressService(),
        )
    assert_true("removed_duplicate_context", len(result.get("removed_context_tasks") or []) == 1, str(result))
    assert_true("web_task_remains", any(task.get("source") == "web_discovery" for task in tasks), str(tasks))
    assert_true("reflection_recorded", any(item.get("action") == "prune_pending_mcagent_context" for item in plan.get("agent_reflections") or []), str(plan))


def test_direct_answer_route_helper_does_not_execute_unselected_delegate() -> None:
    class FakeRun:
        original_question = "ask crawler to collect later"
        question = original_question
        agent = "crawler_agent"
        model = "fake"
        temperature = 0.0
        max_tokens = 100
        is_streaming = False

        def __init__(self) -> None:
            self.config = None
            self.trace = []

        def add_trace(self, stage, status, detail=None):  # noqa: ANN001
            item = {"stage": stage, "status": status, "detail": detail}
            self.trace.append(item)
            return item

        def emit_delta(self, text: str) -> None:
            raise AssertionError(text)

        def response(self, payload: dict[str, Any]) -> dict[str, Any]:
            payload["trace"] = self.trace
            return payload

    original_review = web_server._tool_route_completeness_review
    try:
        web_server._tool_route_completeness_review = lambda *_args, **_kwargs: {  # type: ignore[assignment]
            "missing_side_effect": True,
            "tool": "delegate_crawler",
            "action": "execute_selected_tool",
            "collection_target": "collect public data",
        }
        run = FakeRun()
        executor = web_server.AgentToolExecutor(
            generate_direct_answer=lambda *_args, **_kwargs: "direct answer only",
            generate_direct_answer_stream=lambda *_args, **_kwargs: "direct answer only",
            status_answer=lambda _config: {"answer": "status"},
        )
        result = web_server._handle_direct_answer_route(
            config=make_temp_config(Path(tempfile.gettempdir())),
            agent="crawler_agent",
            model="fake",
            original_question=run.original_question,
            question=run.question,
            tool_decision={"tool": "direct_answer", "collection_target": "collect public data"},
            route_confirmation={},
            action_plan=[],
            executor=executor,
            run=run,
            session_summary={},
            add_trace=run.add_trace,
        )
    finally:
        web_server._tool_route_completeness_review = original_review  # type: ignore[assignment]
    statuses = [(item.get("stage"), item.get("status")) for item in result.get("trace") or []]
    assert_true("direct_answer_returned", result.get("answer") == "direct answer only", str(result))
    assert_true("missing_side_effect_visible", ("plan", "route_completeness_gap") in statuses, str(statuses))
    assert_true("delegate_not_executed", ("decide", "direct_answer_missing_side_effect_not_executed") in statuses, str(statuses))
    assert_true("no_job", not result.get("job"), str(result))


def test_temporary_extract_route_does_not_upgrade_to_delegate_on_confirmation_suggestion() -> None:
    class FakeRun:
        original_question = "summarize https://example.com without saving"
        question = original_question
        agent = "crawler_agent"
        model = "fake"
        temperature = 0.0
        max_tokens = 100
        is_streaming = False

        def __init__(self) -> None:
            self.config = None
            self.trace = []

        def add_trace(self, stage, status, detail=None):  # noqa: ANN001
            item = {"stage": stage, "status": status, "detail": detail}
            self.trace.append(item)
            return item

        def emit_delta(self, text: str) -> None:
            raise AssertionError(text)

        def response(self, payload: dict[str, Any]) -> dict[str, Any]:
            payload["trace"] = self.trace
            return payload

    run = FakeRun()
    executor = web_server.AgentToolExecutor(
        generate_direct_answer=lambda *_args, **_kwargs: "direct after extract cancelled",
        generate_direct_answer_stream=lambda *_args, **_kwargs: "direct after extract cancelled",
        status_answer=lambda _config: {"answer": "status"},
    )
    result = web_server._handle_temporary_extract_route(
        config=make_temp_config(Path(tempfile.gettempdir())),
        agent="crawler_agent",
        model="fake",
        temperature=0.0,
        max_tokens=100,
        original_question=run.original_question,
        question=run.question,
        tool_decision={"tool": "temporary_extract", "collection_target": "https://example.com"},
        route_confirmation={"proceed": False, "tool": "temporary_extract", "suggested_tool": "delegate_crawler", "reason": "persist instead"},
        executor=executor,
        run=run,
        session_summary={},
        trace=run.trace,
        add_trace=run.add_trace,
    )
    statuses = [(item.get("stage"), item.get("status")) for item in result.get("trace") or []]
    assert_true("delegate_suggestion_visible", ("extract", "delegate_suggestion_not_executed") in statuses, str(statuses))
    assert_true("direct_after_cancel", result.get("answer") == "direct after extract cancelled", str(result))
    assert_true("no_job", not result.get("job"), str(result))


def test_inventory_route_confirmation_cannot_upgrade_to_delegate() -> None:
    class FakeRun:
        original_question = "list local data then collect"
        question = original_question
        agent = "mcagent_rag"
        model = "fake"
        temperature = 0.0
        max_tokens = 100
        is_streaming = False

        def __init__(self) -> None:
            self.config = None
            self.trace = []

        def add_trace(self, stage, status, detail=None):  # noqa: ANN001
            item = {"stage": stage, "status": status, "detail": detail}
            self.trace.append(item)
            return item

        def emit_delta(self, text: str) -> None:
            raise AssertionError(text)

        def response(self, payload: dict[str, Any]) -> dict[str, Any]:
            payload["trace"] = self.trace
            return payload

    run = FakeRun()
    executor = web_server.AgentToolExecutor(
        generate_direct_answer=lambda *_args, **_kwargs: "direct after inventory cancelled",
        generate_direct_answer_stream=lambda *_args, **_kwargs: "direct after inventory cancelled",
        status_answer=lambda _config: {"answer": "status"},
    )
    result = web_server._handle_local_corpus_inventory_route(
        config=make_temp_config(Path(tempfile.gettempdir())),
        payload={},
        agent="mcagent_rag",
        model="fake",
        original_question=run.original_question,
        question=run.question,
        tool_decision={"tool": "local_corpus_inventory"},
        route_confirmation={"proceed": False, "tool": "local_corpus_inventory", "suggested_tool": "delegate_crawler"},
        action_plan=[],
        executor=executor,
        run=run,
        session_summary={},
        trace=run.trace,
        add_trace=run.add_trace,
    )
    statuses = [(item.get("stage"), item.get("status")) for item in result.get("trace") or []]
    assert_true("inventory_confirmed", ("retrieve", "inventory_next_step_confirmed") in statuses, str(statuses))
    assert_true("direct_after_cancel", result.get("answer") == "direct after inventory cancelled", str(result))
    assert_true("no_job", not result.get("job"), str(result))


def test_delegate_route_helper_respects_confirmation_cancel() -> None:
    class FakeRun:
        original_question = "collect public docs"
        question = original_question
        agent = "crawler_agent"
        model = "fake"
        temperature = 0.0
        max_tokens = 100
        is_streaming = False

        def __init__(self) -> None:
            self.config = None
            self.trace = []

        def add_trace(self, stage, status, detail=None):  # noqa: ANN001
            item = {"stage": stage, "status": status, "detail": detail}
            self.trace.append(item)
            return item

        def emit_delta(self, text: str) -> None:
            raise AssertionError(text)

        def response(self, payload: dict[str, Any]) -> dict[str, Any]:
            payload["trace"] = self.trace
            return payload

    class FakeRouter:
        def confirm_next_step(self, *_args, **_kwargs):  # noqa: ANN001
            return {"proceed": False, "tool": "direct_answer", "reason": "Agent cancelled side effect."}

    calls: list[str] = []
    original_prepare = web_server._prepare_and_start_crawler_delegation
    try:
        web_server._prepare_and_start_crawler_delegation = lambda *_args, **_kwargs: calls.append("unexpected")  # type: ignore[assignment]
        run = FakeRun()
        executor = web_server.AgentToolExecutor(
            generate_direct_answer=lambda *_args, **_kwargs: "direct after delegate cancelled",
            generate_direct_answer_stream=lambda *_args, **_kwargs: "direct after delegate cancelled",
            status_answer=lambda _config: {"answer": "status"},
        )
        result = web_server._handle_delegate_crawler_route(
            config=make_temp_config(Path(tempfile.gettempdir())),
            payload={},
            agent="crawler_agent",
            model="fake",
            original_question=run.original_question,
            question=run.question,
            tool_decision={"tool": "delegate_crawler", "collection_target": "collect public docs"},
            route_confirmation={},
            action_plan=[],
            collection_request_agent_message=False,
            router=FakeRouter(),  # type: ignore[arg-type]
            executor=executor,
            run=run,
            session_summary={},
            trace=run.trace,
            add_trace=run.add_trace,
        )
    finally:
        web_server._prepare_and_start_crawler_delegation = original_prepare  # type: ignore[assignment]
    statuses = [(item.get("stage"), item.get("status")) for item in result.get("trace") or []]
    assert_true("delegate_confirmation_visible", ("delegate", "next_step_confirmed") in statuses, str(statuses))
    assert_true("direct_after_cancel", result.get("answer") == "direct after delegate cancelled", str(result))
    assert_true("no_prepare_call", calls == [], str(calls))
    assert_true("no_job", not result.get("job"), str(result))


def test_crawler_action_plan_delegate_route_starts_selected_job() -> None:
    calls: list[dict[str, Any]] = []

    class Plan:
        requested_by = "user"
        delivery_target = "MCagent/RAG"
        collection_question = "collect Kotlin coroutine docs"
        handoff_brief = "CrawlerAgent selected delegate_crawler in its own action_plan."

    def fake_prepare(*_args, **kwargs):  # noqa: ANN001
        calls.append(dict(kwargs))
        job = web_server.Job(id="selected-plan-job", kind="crawler", title=kwargs.get("collection_question", ""), status="queued", summary="queued")
        return web_server.CrawlerDelegationRun(plan=Plan(), job=job, created=True, note="\n\njob started")

    trace: list[dict[str, Any]] = []

    def add_trace(stage, status, detail=None):  # noqa: ANN001
        item = {"stage": stage, "status": status, "detail": detail}
        trace.append(item)
        return item

    original_prepare = web_server._prepare_and_start_crawler_delegation
    try:
        web_server._prepare_and_start_crawler_delegation = fake_prepare  # type: ignore[assignment]
        action_plan = [
            {"step": 1, "tool": "web_search", "goal": "discover public sources"},
            {"step": 2, "tool": "delegate_crawler", "goal": "run background collection"},
        ]
        result = web_server._handle_crawler_action_plan_delegate_route(
            config=make_temp_config(Path(tempfile.gettempdir())),
            payload={"delivery_target": "MCagent/RAG"},
            agent="crawler_agent",
            model="fake",
            original_question="collect Kotlin coroutine docs",
            question="collect Kotlin coroutine docs",
            tool_decision={"tool": "planned_workflow", "collection_target": "collect Kotlin coroutine docs", "delivery_target": "MCagent/RAG"},
            action_plan=action_plan,
            session_summary={},
            trace=trace,
            add_trace=add_trace,
        )
    finally:
        web_server._prepare_and_start_crawler_delegation = original_prepare  # type: ignore[assignment]
    statuses = [(item.get("stage"), item.get("status")) for item in result.get("trace") or []]
    assert_true("selected_step_trace", ("plan", "executing_agent_selected_step") in statuses, str(statuses))
    assert_true("prepare_called_once", len(calls) == 1, str(calls))
    assert_true("action_plan_forwarded", calls[0].get("action_plan") == action_plan, str(calls[0]))
    assert_true("job_returned", (result.get("job") or {}).get("id") == "selected-plan-job", str(result))
    assert_true("selected_action_plan_visible", (result.get("delegation") or {}).get("selected_action_plan") == action_plan, str(result))


def test_mcagent_inventory_planned_workflow_delegates_only_selected_step() -> None:
    calls: list[dict[str, Any]] = []

    class Plan:
        requested_by = "user_via_mcagent"
        delivery_target = "MCagent/RAG"
        collection_question = "collect missing local corpus evidence"
        handoff_brief = "MCagent selected inventory first, then delegate."

    def fake_inventory(_config, _question):  # noqa: ANN001
        return {
            "answer": "本地资料盘点：已有基础介绍，缺少进阶玩法资料。",
            "sources": [{"title": "local inventory", "document_id": 1}],
            "context": "inventory context",
            "agent": "mcagent_rag",
        }

    def fake_prepare(*_args, **kwargs):  # noqa: ANN001
        calls.append(dict(kwargs))
        job = web_server.Job(id="inventory-plan-job", kind="crawler", title=kwargs.get("collection_question", ""), status="queued", summary="queued")
        return web_server.CrawlerDelegationRun(plan=Plan(), job=job, created=True, note="\n\njob started")

    trace: list[dict[str, Any]] = []

    def add_trace(stage, status, detail=None):  # noqa: ANN001
        item = {"stage": stage, "status": status, "detail": detail}
        trace.append(item)
        return item

    original_inventory = web_server._local_corpus_inventory_answer
    original_prepare = web_server._prepare_and_start_crawler_delegation
    try:
        web_server._local_corpus_inventory_answer = fake_inventory  # type: ignore[assignment]
        web_server._prepare_and_start_crawler_delegation = fake_prepare  # type: ignore[assignment]
        action_plan = [
            {"step": 1, "tool": "local_corpus_inventory", "goal": "inspect local coverage"},
            {"step": 2, "tool": "delegate_crawler", "goal": "collect missing evidence"},
        ]
        result = web_server._handle_mcagent_inventory_planned_workflow_route(
            config=make_temp_config(Path(tempfile.gettempdir())),
            payload={"delivery_target": "MCagent/RAG"},
            agent="mcagent_rag",
            model="fake",
            original_question="本地有哪些资料，缺的让 Crawler 补",
            question="本地有哪些资料，缺的让 Crawler 补",
            tool_decision={"tool": "planned_workflow", "collection_target": "collect missing local corpus evidence", "delivery_target": "MCagent/RAG"},
            action_plan=action_plan,
            session_summary={},
            trace=trace,
            add_trace=add_trace,
        )
    finally:
        web_server._local_corpus_inventory_answer = original_inventory  # type: ignore[assignment]
        web_server._prepare_and_start_crawler_delegation = original_prepare  # type: ignore[assignment]
    statuses = [(item.get("stage"), item.get("status")) for item in result.get("trace") or []]
    assert_true("inventory_step_visible", ("plan", "executing_agent_selected_step") in statuses, str(statuses))
    assert_true("prepare_called_once", len(calls) == 1, str(calls))
    assert_true("action_plan_forwarded", calls[0].get("action_plan") == action_plan, str(calls[0]))
    assert_true("gap_summary_from_inventory", "缺少进阶玩法资料" in str(calls[0].get("gap_summary") or ""), str(calls[0]))
    assert_true("job_returned", (result.get("job") or {}).get("id") == "inventory-plan-job", str(result))
    assert_true("planned_workflow_trace", any((item.get("detail") or {}).get("planned_workflow_executed") for item in result.get("trace") or []), str(result.get("trace")))


def test_no_retrieval_results_without_selected_delegate_does_not_start_job() -> None:
    calls: list[str] = []
    trace: list[dict[str, Any]] = []

    def add_trace(stage, status, detail=None):  # noqa: ANN001
        item = {"stage": stage, "status": status, "detail": detail}
        trace.append(item)
        return item

    original_prepare = web_server._prepare_and_start_crawler_delegation
    try:
        web_server._prepare_and_start_crawler_delegation = lambda *_args, **_kwargs: calls.append("unexpected")  # type: ignore[assignment]
        result = web_server._handle_no_retrieval_results(
            config=make_temp_config(Path(tempfile.gettempdir())),
            payload={},
            agent="mcagent_rag",
            model="fake",
            original_question="本地没有资料怎么办",
            question="本地没有资料怎么办",
            tool_decision={"tool": "answer"},
            action_plan=[],
            planned_delegate=False,
            evidence_question="本地没有资料怎么办",
            session_summary={},
            trace=trace,
            add_trace=add_trace,
        )
    finally:
        web_server._prepare_and_start_crawler_delegation = original_prepare  # type: ignore[assignment]
    statuses = [(item.get("stage"), item.get("status")) for item in result.get("trace") or []]
    assert_true("insufficient_trace", ("done", "insufficient_evidence") in statuses, str(statuses))
    assert_true("no_prepare_call", calls == [], str(calls))
    assert_true("no_job", not result.get("job"), str(result))
    assert_true("no_auto_crawler_text", "本轮不会自动通知 Crawler" in str(result.get("answer") or ""), str(result))


def test_no_retrieval_results_with_selected_delegate_starts_job() -> None:
    calls: list[dict[str, Any]] = []

    class Plan:
        requested_by = "mcagent"
        delivery_target = "MCagent/RAG"
        collection_question = "collect missing evidence"
        handoff_brief = "planned delegate after empty retrieval"

    def fake_prepare(*_args, **kwargs):  # noqa: ANN001
        calls.append(dict(kwargs))
        job = web_server.Job(id="empty-rag-delegate-job", kind="crawler", title=kwargs.get("collection_question", ""), status="queued", summary="queued")
        return web_server.CrawlerDelegationRun(plan=Plan(), job=job, created=True, note="\n\njob started")

    trace: list[dict[str, Any]] = []

    def add_trace(stage, status, detail=None):  # noqa: ANN001
        item = {"stage": stage, "status": status, "detail": detail}
        trace.append(item)
        return item

    original_prepare = web_server._prepare_and_start_crawler_delegation
    try:
        web_server._prepare_and_start_crawler_delegation = fake_prepare  # type: ignore[assignment]
        action_plan = [{"step": 1, "tool": "local_rag_search"}, {"step": 2, "tool": "delegate_crawler"}]
        result = web_server._handle_no_retrieval_results(
            config=make_temp_config(Path(tempfile.gettempdir())),
            payload={"delivery_target": "MCagent/RAG"},
            agent="mcagent_rag",
            model="fake",
            original_question="检索后缺资料就补库",
            question="检索后缺资料就补库",
            tool_decision={"tool": "planned_workflow", "collection_target": "collect missing evidence", "delivery_target": "MCagent/RAG"},
            action_plan=action_plan,
            planned_delegate=True,
            evidence_question="检索后缺资料就补库",
            session_summary={},
            trace=trace,
            add_trace=add_trace,
        )
    finally:
        web_server._prepare_and_start_crawler_delegation = original_prepare  # type: ignore[assignment]
    assert_true("prepare_called_once", len(calls) == 1, str(calls))
    assert_true("action_plan_forwarded", calls[0].get("action_plan") == action_plan, str(calls[0]))
    assert_true("job_returned", (result.get("job") or {}).get("id") == "empty-rag-delegate-job", str(result))


def main() -> int:
    test_conversation_graph_routes_only_by_message_target()
    test_conversation_graph_can_dispatch_to_crawler_node()
    test_non_streaming_graph_reuses_checkpointed_runtime_without_reusing_emit()
    test_agent_subgraphs_load_session_memory_context()
    test_crawler_background_job_enters_langgraph_runtime()
    test_crawler_job_plan_preparation_is_objective_and_reusable()
    test_crawler_task_preparation_routes_archive_urls_objectively()
    test_crawler_task_result_metadata_is_recorded_objectively()
    test_crawler_task_accounting_inserts_archive_internal_followup()
    test_crawler_task_accounting_does_not_duplicate_followup()
    test_mcagent_context_followup_extends_budget_for_external_collection()
    test_crawler_loop_executes_materialized_tasks_before_reflecting_again()
    test_crawler_task_accounting_turns_archive_fetch_observation_into_download_followup()
    test_crawler_task_step_blocks_empty_query_before_tool_execution()
    test_crawler_task_step_executes_command_and_records_accounting()
    test_crawler_task_step_ignores_unbacked_tool_record_claims()
    test_crawler_loop_control_finishes_after_rag_success_checkpoint()
    test_crawler_loop_does_not_finish_when_guide_coverage_unmet()
    test_crawler_reflection_helper_returns_objective_contract_feedback()
    test_crawler_after_task_review_prunes_duplicate_mcagent_context()
    test_direct_answer_route_helper_does_not_execute_unselected_delegate()
    test_temporary_extract_route_does_not_upgrade_to_delegate_on_confirmation_suggestion()
    test_inventory_route_confirmation_cannot_upgrade_to_delegate()
    test_delegate_route_helper_respects_confirmation_cancel()
    test_crawler_action_plan_delegate_route_starts_selected_job()
    test_mcagent_inventory_planned_workflow_delegates_only_selected_step()
    test_no_retrieval_results_without_selected_delegate_does_not_start_job()
    test_no_retrieval_results_with_selected_delegate_starts_job()
    print("LANGGRAPH RUNTIME SCENARIOS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
