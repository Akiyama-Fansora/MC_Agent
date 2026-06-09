from __future__ import annotations

from typing import Any, Callable

from langgraph.graph import END, START, StateGraph

from ..agent_runtime import domain_collection_tools_for_crawler, general_collection_tools_for_crawler
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


def build_crawler_graph(config: AppConfig, agent_delivery: AgentDeliveryFn, emit: EmitFn | None = None):
    builder = StateGraph(AgentGraphState)

    def receive(state: AgentGraphState) -> dict[str, Any]:
        payload = dict(state.get("payload") or {})
        payload["agent"] = "crawler_agent"
        general_tools = [tool.name for tool in general_collection_tools_for_crawler()]
        minecraft_tools = [tool.name for tool in domain_collection_tools_for_crawler("minecraft")]
        return {
            "agent_id": "crawler_agent",
            "payload": payload,
            "tool_boundary": {
                "agent": "CrawlerAgent",
                "allowed_capability_groups": [
                    "agent_message",
                    "web_discovery",
                    "fetch_url",
                    "browser_render",
                    "local_file_read",
                    "download",
                    "archive_extract",
                    "artifact_save",
                    "rag_ingest",
                    "optional_domain_toolsets",
                ],
                "general_collection_tools": general_tools,
                "domain_toolsets": {"minecraft": minecraft_tools},
                "principle": "Tools expose objective observations; CrawlerAgent LLM owns source graph, tool choice, observation review, retry/accept/reject, persistence, and final report.",
            },
            **_append(state, "crawler.receive", "message_received", {"agent": "crawler_agent"}),
        }

    def understand_boundary(state: AgentGraphState) -> dict[str, Any]:
        boundary = dict(state.get("tool_boundary") or {})
        thread_id = str(state.get("thread_id") or (state.get("payload") or {}).get("session_id") or "default")
        memory = DEFAULT_SESSION_STORE.context(thread_id, agent="crawler_agent").to_dict()
        return {
            "tool_boundary": boundary,
            "memory_context": memory,
            **_append(
                state,
                "crawler.understand_boundary",
                "general_domain_boundary_declared",
                {
                    "allowed_capability_groups": boundary.get("allowed_capability_groups", []),
                    "session_id": memory.get("session_id"),
                    "turn_count": memory.get("turn_count"),
                },
            ),
        }

    def select_tool_groups(state: AgentGraphState) -> dict[str, Any]:
        boundary = dict(state.get("tool_boundary") or {})
        selected = {
            "default_groups": ["general"],
            "default_tools": list(boundary.get("general_collection_tools") or []),
            "candidate_domain_toolsets": dict(boundary.get("domain_toolsets") or {}),
            "decision_owner": "CrawlerAgent LLM",
            "selection_contract": (
                "The graph exposes general tools by default and candidate domain toolsets as options. "
                "It does not decide semantic relevance or enable a domain plugin on the CrawlerAgent's behalf."
            ),
        }
        return {
            "selected_tool_groups": selected,
            **_append(
                state,
                "crawler.select_tool_groups",
                "default_general_candidates_exposed",
                {
                    "default_groups": selected["default_groups"],
                    "candidate_domain_toolsets": list(selected["candidate_domain_toolsets"].keys()),
                    "decision_owner": selected["decision_owner"],
                },
            ),
        }

    def prepare_mission_contract(state: AgentGraphState) -> dict[str, Any]:
        payload = dict(state.get("payload") or {})
        selected_groups = dict(state.get("selected_tool_groups") or {})
        message = payload.get("agent_message") if isinstance(payload.get("agent_message"), dict) else {}
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        contract = {
            "question": str(payload.get("question") or payload.get("query") or ""),
            "from_agent": str(message.get("from_agent") or payload.get("message_from") or ""),
            "delivery_target": str(metadata.get("delivery_target") or payload.get("delivery_target") or ""),
            "default_tools": list(selected_groups.get("default_tools") or []),
            "candidate_domain_toolsets": dict(selected_groups.get("candidate_domain_toolsets") or {}),
            "decision_owner": "CrawlerAgent LLM",
            "objective_contract": (
                "The graph records the mission facts and available tool groups. "
                "CrawlerAgent still owns source graph construction, domain choice, tool execution order, observation review, and persistence decisions."
            ),
        }
        return {
            "mission_contract": contract,
            **_append(
                state,
                "crawler.prepare_mission_contract",
                "mission_contract_exposed",
                {
                    "from_agent": contract["from_agent"],
                    "delivery_target": contract["delivery_target"],
                    "decision_owner": contract["decision_owner"],
                },
            ),
        }

    def run_legacy_adapter(state: AgentGraphState) -> dict[str, Any]:
        result = deliver_via_legacy_runtime(
            config,
            dict(state.get("payload") or {}),
            agent_delivery=agent_delivery,
            emit=emit,
            agent_id="crawler_agent",
            graph_name="CrawlerAgentGraph",
            node_name="crawler.legacy_adapter",
        )
        return {
            "result": result,
            "runtime_adapter": result.get("legacy_runtime_adapter") or {},
            **_append(
                state,
                "crawler.legacy_adapter",
                "delegated_to_legacy_runtime_adapter",
                {"agent": "crawler_agent", "adapter": "legacy_web_server_runtime"},
            ),
        }

    def finalize(state: AgentGraphState) -> dict[str, Any]:
        result = dict(state.get("result") or {})
        agent_runtime = {
            "agent_graph": "CrawlerAgentGraph",
            "agent_id": "crawler_agent",
            "tool_boundary": state.get("tool_boundary") or {},
            "selected_tool_groups": state.get("selected_tool_groups") or {},
            "memory_context": state.get("memory_context") or {},
            "mission_contract": state.get("mission_contract") or {},
            "runtime_adapter": state.get("runtime_adapter") or result.get("legacy_runtime_adapter") or {},
            "visited_nodes": [*state.get("visited_nodes", []), "crawler.finalize"],
            "events": [*state.get("graph_events", []), _event("crawler.finalize", "ready")],
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
    builder.add_node("understand_boundary", understand_boundary)
    builder.add_node("select_tool_groups", select_tool_groups)
    builder.add_node("prepare_mission_contract", prepare_mission_contract)
    builder.add_node("legacy_adapter", run_legacy_adapter)
    builder.add_node("finalize", finalize)
    builder.add_edge(START, "receive")
    builder.add_edge("receive", "understand_boundary")
    builder.add_edge("understand_boundary", "select_tool_groups")
    builder.add_edge("select_tool_groups", "prepare_mission_contract")
    builder.add_edge("prepare_mission_contract", "legacy_adapter")
    builder.add_edge("legacy_adapter", "finalize")
    builder.add_edge("finalize", END)
    return builder.compile()


def run_crawler_graph(
    config: AppConfig,
    payload: dict[str, Any],
    *,
    agent_delivery: AgentDeliveryFn,
    emit: EmitFn | None = None,
    thread_id: str = "default",
) -> dict[str, Any]:
    graph = build_crawler_graph(config, agent_delivery, emit=emit)
    final_state = graph.invoke(
        {
            "thread_id": thread_id,
            "agent_id": "crawler_agent",
            "payload": dict(payload),
            "tool_boundary": {},
            "graph_events": [],
            "visited_nodes": [],
            "errors": [],
        }
    )
    return dict(final_state.get("result") or {})
