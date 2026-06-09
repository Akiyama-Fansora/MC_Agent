from __future__ import annotations

from typing import Any


GRAPH_STATUS_ROUTE_EXECUTOR = "graph_status_route_executor"


def graph_status_route_executor_metadata(
    *,
    agent_id: str,
    graph_name: str,
    node_name: str,
    runtime_request: dict[str, Any] | None = None,
    route_decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    runtime_request = runtime_request if isinstance(runtime_request, dict) else {}
    route_decision = route_decision if isinstance(route_decision, dict) else {}
    display_agent = "CrawlerAgent" if agent_id == "crawler_agent" else "MCagent" if agent_id == "mcagent_rag" else agent_id
    return {
        "adapter": GRAPH_STATUS_ROUTE_EXECUTOR,
        "agent_id": agent_id,
        "graph": graph_name,
        "node": node_name,
        "runtime_request_id": runtime_request.get("request_id") or "",
        "contract_kind": runtime_request.get("contract_kind") or "",
        "session_id": runtime_request.get("session_id") or "",
        "route_decision_id": route_decision.get("route_decision_id") or "",
        "route_intent": route_decision.get("route_intent") or "",
        "migration_status": "graph_status_route_migrated",
        "decision_owner": f"{display_agent} LLM",
        "route_decision_executed_by_graph": bool(route_decision.get("routed")),
        "legacy_runtime_adapter_bypassed": True,
        "side_effect_executed": False,
        "objective_boundary": (
            "The graph executed only the already Agent-selected, side-effect-free status route. "
            "It did not choose a tool, start jobs, judge evidence, persist data, or alter AgentMessage routing."
        ),
    }
