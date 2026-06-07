from __future__ import annotations

from typing import Any, Callable

from langgraph.graph import END, START, StateGraph

from ..agent_runtime import tools_for_agent
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


def build_mcagent_graph(config: AppConfig, legacy_delivery: LegacyDeliveryFn, emit: EmitFn | None = None):
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
        return {
            "tool_boundary": boundary,
            **_append(
                state,
                "mcagent.load_memory_boundary",
                "local_only_boundary_declared",
                {"allowed_capability_groups": boundary.get("allowed_capability_groups", [])},
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

    def run_legacy_local_agent(state: AgentGraphState) -> dict[str, Any]:
        result = legacy_delivery(config, dict(state.get("payload") or {}), emit=emit)
        return {
            "result": result,
            **_append(state, "mcagent.legacy_runtime", "completed", {"agent": "mcagent_rag"}),
        }

    def finalize(state: AgentGraphState) -> dict[str, Any]:
        result = dict(state.get("result") or {})
        agent_runtime = {
            "agent_graph": "MCagentGraph",
            "agent_id": "mcagent_rag",
            "tool_boundary": state.get("tool_boundary") or {},
            "selected_tool_groups": state.get("selected_tool_groups") or {},
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
    builder.add_node("legacy_runtime", run_legacy_local_agent)
    builder.add_node("finalize", finalize)
    builder.add_edge(START, "receive")
    builder.add_edge("receive", "load_memory_boundary")
    builder.add_edge("load_memory_boundary", "select_local_tools")
    builder.add_edge("select_local_tools", "legacy_runtime")
    builder.add_edge("legacy_runtime", "finalize")
    builder.add_edge("finalize", END)
    return builder.compile()


def run_mcagent_graph(
    config: AppConfig,
    payload: dict[str, Any],
    *,
    legacy_delivery: LegacyDeliveryFn,
    emit: EmitFn | None = None,
    thread_id: str = "default",
) -> dict[str, Any]:
    graph = build_mcagent_graph(config, legacy_delivery, emit=emit)
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
