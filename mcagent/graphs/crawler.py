from __future__ import annotations

from typing import Any, Callable

from langgraph.graph import END, START, StateGraph

from ..agent_runtime import domain_collection_tools_for_crawler, general_collection_tools_for_crawler
from ..config import AppConfig
from .agent_state import AgentGraphState
from .state import GraphEvent


EmitFn = Callable[[str, Any], None]
LegacyDeliveryFn = Callable[..., dict[str, Any]]


def _event(node: str, status: str, detail: dict[str, Any] | None = None) -> GraphEvent:
    return {"node": node, "status": status, "detail": dict(detail or {})}


def _append(state: AgentGraphState, node: str, status: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "visited_nodes": [*state.get("visited_nodes", []), node],
        "graph_events": [*state.get("graph_events", []), _event(node, status, detail)],
    }


def build_crawler_graph(config: AppConfig, legacy_delivery: LegacyDeliveryFn, emit: EmitFn | None = None):
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
        return {
            "tool_boundary": boundary,
            **_append(
                state,
                "crawler.understand_boundary",
                "general_domain_boundary_declared",
                {"allowed_capability_groups": boundary.get("allowed_capability_groups", [])},
            ),
        }

    def run_legacy_crawler_agent(state: AgentGraphState) -> dict[str, Any]:
        result = legacy_delivery(config, dict(state.get("payload") or {}), emit=emit)
        return {
            "result": result,
            **_append(state, "crawler.legacy_runtime", "completed", {"agent": "crawler_agent"}),
        }

    def finalize(state: AgentGraphState) -> dict[str, Any]:
        result = dict(state.get("result") or {})
        agent_runtime = {
            "agent_graph": "CrawlerAgentGraph",
            "agent_id": "crawler_agent",
            "tool_boundary": state.get("tool_boundary") or {},
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
    builder.add_node("legacy_runtime", run_legacy_crawler_agent)
    builder.add_node("finalize", finalize)
    builder.add_edge(START, "receive")
    builder.add_edge("receive", "understand_boundary")
    builder.add_edge("understand_boundary", "legacy_runtime")
    builder.add_edge("legacy_runtime", "finalize")
    builder.add_edge("finalize", END)
    return builder.compile()


def run_crawler_graph(
    config: AppConfig,
    payload: dict[str, Any],
    *,
    legacy_delivery: LegacyDeliveryFn,
    emit: EmitFn | None = None,
    thread_id: str = "default",
) -> dict[str, Any]:
    graph = build_crawler_graph(config, legacy_delivery, emit=emit)
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
