from __future__ import annotations

from typing import Any, Callable

from langgraph.graph import END, START, StateGraph

from ..agent_runtime import tools_for_agent
from ..config import AppConfig
from ..session_state import DEFAULT_SESSION_STORE
from .agent_state import AgentGraphState
from .legacy_adapter import deliver_via_legacy_runtime
from .state import GraphEvent


EmitFn = Callable[[str, Any], None]
AgentDeliveryFn = Callable[..., dict[str, Any]]


def _event(node: str, status: str, detail: dict[str, Any] | None = None) -> GraphEvent:
    return {"node": node, "status": status, "detail": dict(detail or {})}


def _append(state: AgentGraphState, node: str, status: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "visited_nodes": [*state.get("visited_nodes", []), node],
        "graph_events": [*state.get("graph_events", []), _event(node, status, detail)],
    }


def build_mcagent_graph(config: AppConfig, agent_delivery: AgentDeliveryFn, emit: EmitFn | None = None):
    builder = StateGraph(AgentGraphState)

    def receive(state: AgentGraphState) -> dict[str, Any]:
        payload = dict(state.get("payload") or {})
        payload["agent"] = "mcagent_rag"
        local_tools = [tool.name for tool in tools_for_agent("mcagent_rag")]
        return {
            "agent_id": "mcagent_rag",
            "payload": payload,
            "tool_boundary": {
                "agent": "MCagent",
                "allowed_capability_groups": ["local_rag", "local_session_memory", "agent_message"],
                "blocked_capability_groups": ["web_search", "browser", "download", "archive_extract", "public_web_ingest"],
                "local_tools": local_tools,
                "principle": "MCagent answers only from local data/RAG and may request CrawlerAgent through AgentMessage when local evidence is insufficient.",
            },
            **_append(state, "mcagent.receive", "message_received", {"agent": "mcagent_rag"}),
        }

    def load_memory_boundary(state: AgentGraphState) -> dict[str, Any]:
        boundary = dict(state.get("tool_boundary") or {})
        thread_id = str(state.get("thread_id") or (state.get("payload") or {}).get("session_id") or "default")
        memory = DEFAULT_SESSION_STORE.context(thread_id, agent="mcagent_rag").to_dict()
        return {
            "tool_boundary": boundary,
            "memory_context": memory,
            **_append(
                state,
                "mcagent.load_memory_boundary",
                "local_only_boundary_declared",
                {
                    "allowed_capability_groups": boundary.get("allowed_capability_groups", []),
                    "session_id": memory.get("session_id"),
                    "turn_count": memory.get("turn_count"),
                },
            ),
        }

    def select_local_tools(state: AgentGraphState) -> dict[str, Any]:
        boundary = dict(state.get("tool_boundary") or {})
        selected = {
            "default_groups": ["local"],
            "default_tools": list(boundary.get("local_tools") or []),
            "blocked_groups": list(boundary.get("blocked_capability_groups") or []),
            "decision_owner": "MCagent LLM",
            "selection_contract": (
                "MCagent may choose only local/RAG/status/message tools. "
                "Network, browser, download, archive extraction, and public web ingest tools are not registered to this graph."
            ),
        }
        return {
            "selected_tool_groups": selected,
            **_append(
                state,
                "mcagent.select_local_tools",
                "local_tools_exposed",
                {
                    "default_groups": selected["default_groups"],
                    "blocked_groups": selected["blocked_groups"],
                    "decision_owner": selected["decision_owner"],
                },
            ),
        }

    def prepare_local_retrieval(state: AgentGraphState) -> dict[str, Any]:
        payload = dict(state.get("payload") or {})
        contract = {
            "question": str(payload.get("question") or payload.get("query") or ""),
            "allowed_evidence_sources": ["local_rag", "local_corpus_inventory", "session_memory"],
            "blocked_evidence_sources": ["public_web", "browser", "downloaded_archive_without_crawler"],
            "decision_owner": "MCagent LLM",
            "objective_contract": (
                "The graph exposes the local evidence boundary. Retrieval candidates and evidence sufficiency "
                "must still be judged by MCagent's LLM or the legacy MCagent runtime during migration."
            ),
        }
        return {
            "retrieval_contract": contract,
            **_append(
                state,
                "mcagent.prepare_local_retrieval",
                "local_evidence_contract_exposed",
                {
                    "allowed_evidence_sources": contract["allowed_evidence_sources"],
                    "blocked_evidence_sources": contract["blocked_evidence_sources"],
                    "decision_owner": contract["decision_owner"],
                },
            ),
        }

    def prepare_route_input_contract(state: AgentGraphState) -> dict[str, Any]:
        payload = dict(state.get("payload") or {})
        thread_id = str(state.get("thread_id") or payload.get("session_id") or "default")
        message = payload.get("agent_message") if isinstance(payload.get("agent_message"), dict) else {}
        selected_groups = dict(state.get("selected_tool_groups") or {})
        route_input_contract = {
            "contract_id": f"{thread_id}:mcagent_rag:route_input",
            "node": "mcagent.prepare_route_input_contract",
            "graph": "MCagentGraph",
            "agent_id": "mcagent_rag",
            "session_id": str(payload.get("session_id") or thread_id),
            "contract_kind": "mcagent_route_input_contract",
            "original_question": str(payload.get("question") or payload.get("query") or ""),
            "contextual_question_hint": str(payload.get("question") or payload.get("query") or ""),
            "message": {
                "from_agent": str(message.get("from_agent") or payload.get("message_from") or "User"),
                "to_agent": str(message.get("to_agent") or "MCagent"),
                "intent": str(message.get("intent") or ""),
                "has_agent_message": bool(message),
            },
            "candidate_route_tools": list(selected_groups.get("default_tools") or []),
            "blocked_capability_groups": list((state.get("tool_boundary") or {}).get("blocked_capability_groups") or []),
            "session_memory": state.get("memory_context") or {},
            "retrieval_contract": state.get("retrieval_contract") or {},
            "decision_owner": "MCagent LLM",
            "objective_contract": (
                "The graph prepares objective inputs for the later tool-routing decision. "
                "It does not select a tool, create a route_intent, or confirm side effects."
            ),
        }
        return {
            "route_input_contract": route_input_contract,
            **_append(
                state,
                "mcagent.prepare_route_input_contract",
                "route_input_contract_prepared",
                {
                    "contract_id": route_input_contract["contract_id"],
                    "contract_kind": route_input_contract["contract_kind"],
                    "candidate_route_tool_count": len(route_input_contract["candidate_route_tools"]),
                    "decision_owner": route_input_contract["decision_owner"],
                },
            ),
        }

    def prepare_runtime_request(state: AgentGraphState) -> dict[str, Any]:
        payload = dict(state.get("payload") or {})
        thread_id = str(state.get("thread_id") or payload.get("session_id") or "default")
        message = payload.get("agent_message") if isinstance(payload.get("agent_message"), dict) else {}
        route_input_contract = state.get("route_input_contract") if isinstance(state.get("route_input_contract"), dict) else {}
        runtime_request = {
            "request_id": f"{thread_id}:mcagent_rag:runtime_request",
            "node": "mcagent.prepare_runtime_request",
            "graph": "MCagentGraph",
            "agent_id": "mcagent_rag",
            "session_id": str(payload.get("session_id") or thread_id),
            "contract_kind": "mcagent_local_runtime_request",
            "payload": payload,
            "message": {
                "from_agent": str(message.get("from_agent") or payload.get("message_from") or "User"),
                "to_agent": str(message.get("to_agent") or "MCagent"),
                "intent": str(message.get("intent") or ""),
                "has_agent_message": bool(message),
            },
            "tool_boundary": state.get("tool_boundary") or {},
            "selected_tool_groups": state.get("selected_tool_groups") or {},
            "memory_context": state.get("memory_context") or {},
            "retrieval_contract": state.get("retrieval_contract") or {},
            "route_input_contract": route_input_contract,
            "route_input_contract_id": route_input_contract.get("contract_id") or "",
            "decision_owner": "MCagent LLM",
            "objective_contract": (
                "The graph prepares objective runtime inputs for MCagent. It does not choose the final tool, "
                "judge evidence sufficiency, or write the natural-language answer."
            ),
        }
        return {
            "runtime_request": runtime_request,
            **_append(
                state,
                "mcagent.prepare_runtime_request",
                "runtime_request_prepared",
                {
                    "request_id": runtime_request["request_id"],
                    "contract_kind": runtime_request["contract_kind"],
                    "decision_owner": runtime_request["decision_owner"],
                },
            ),
        }

    def run_legacy_adapter(state: AgentGraphState) -> dict[str, Any]:
        runtime_request = state.get("runtime_request") if isinstance(state.get("runtime_request"), dict) else {}
        result = deliver_via_legacy_runtime(
            config,
            dict(state.get("payload") or {}),
            agent_delivery=agent_delivery,
            emit=emit,
            agent_id="mcagent_rag",
            graph_name="MCagentGraph",
            node_name="mcagent.legacy_adapter",
            runtime_request=runtime_request,
        )
        return {
            "result": result,
            "runtime_adapter": result.get("legacy_runtime_adapter") or {},
            **_append(
                state,
                "mcagent.legacy_adapter",
                "delegated_to_legacy_runtime_adapter",
                {"agent": "mcagent_rag", "adapter": "legacy_web_server_runtime"},
            ),
        }

    def finalize(state: AgentGraphState) -> dict[str, Any]:
        result = dict(state.get("result") or {})
        agent_runtime = {
            "agent_graph": "MCagentGraph",
            "agent_id": "mcagent_rag",
            "tool_boundary": state.get("tool_boundary") or {},
            "selected_tool_groups": state.get("selected_tool_groups") or {},
            "memory_context": state.get("memory_context") or {},
            "retrieval_contract": state.get("retrieval_contract") or {},
            "route_input_contract": state.get("route_input_contract") or {},
            "runtime_request": state.get("runtime_request") or {},
            "runtime_adapter": state.get("runtime_adapter") or result.get("legacy_runtime_adapter") or {},
            "visited_nodes": [*state.get("visited_nodes", []), "mcagent.finalize"],
            "events": [*state.get("graph_events", []), _event("mcagent.finalize", "ready")],
        }
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        metadata = {**metadata, "agent_graph_runtime": agent_runtime}
        result["metadata"] = metadata
        result["agent_graph_runtime"] = agent_runtime
        return {
            "result": result,
            "visited_nodes": agent_runtime["visited_nodes"],
            "graph_events": agent_runtime["events"],
        }

    builder.add_node("receive", receive)
    builder.add_node("load_memory_boundary", load_memory_boundary)
    builder.add_node("select_local_tools", select_local_tools)
    builder.add_node("prepare_local_retrieval", prepare_local_retrieval)
    builder.add_node("prepare_route_input_contract", prepare_route_input_contract)
    builder.add_node("prepare_runtime_request", prepare_runtime_request)
    builder.add_node("legacy_adapter", run_legacy_adapter)
    builder.add_node("finalize", finalize)
    builder.add_edge(START, "receive")
    builder.add_edge("receive", "load_memory_boundary")
    builder.add_edge("load_memory_boundary", "select_local_tools")
    builder.add_edge("select_local_tools", "prepare_local_retrieval")
    builder.add_edge("prepare_local_retrieval", "prepare_route_input_contract")
    builder.add_edge("prepare_route_input_contract", "prepare_runtime_request")
    builder.add_edge("prepare_runtime_request", "legacy_adapter")
    builder.add_edge("legacy_adapter", "finalize")
    builder.add_edge("finalize", END)
    return builder.compile()


def run_mcagent_graph(
    config: AppConfig,
    payload: dict[str, Any],
    *,
    agent_delivery: AgentDeliveryFn,
    emit: EmitFn | None = None,
    thread_id: str = "default",
) -> dict[str, Any]:
    graph = build_mcagent_graph(config, agent_delivery, emit=emit)
    final_state = graph.invoke(
        {
            "thread_id": thread_id,
            "agent_id": "mcagent_rag",
            "payload": dict(payload),
            "tool_boundary": {},
            "graph_events": [],
            "visited_nodes": [],
            "errors": [],
        }
    )
    return dict(final_state.get("result") or {})
