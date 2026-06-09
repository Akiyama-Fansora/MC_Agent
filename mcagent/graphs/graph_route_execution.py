from __future__ import annotations

from typing import Any


GRAPH_STATUS_ROUTE_EXECUTOR = "graph_status_route_executor"
GRAPH_CRAWLER_AUDIT_ROUTE_EXECUTOR = "graph_crawler_audit_route_executor"
GRAPH_LOCAL_CORPUS_INVENTORY_ROUTE_EXECUTOR = "graph_local_corpus_inventory_route_executor"


def _display_agent(agent_id: str) -> str:
    return "CrawlerAgent" if agent_id == "crawler_agent" else "MCagent" if agent_id == "mcagent_rag" else agent_id


def graph_route_decision_allows_execution(route_decision: dict[str, Any] | None) -> bool:
    route_decision = route_decision if isinstance(route_decision, dict) else {}
    confirmation = route_decision.get("route_confirmation") if isinstance(route_decision.get("route_confirmation"), dict) else {}
    return bool(confirmation.get("proceed", True))


def graph_route_decision_has_action_tool(route_decision: dict[str, Any] | None, tool_name: str) -> bool:
    route_decision = route_decision if isinstance(route_decision, dict) else {}
    wanted = tool_name.strip().lower()
    action_plan = route_decision.get("action_plan") if isinstance(route_decision.get("action_plan"), list) else []
    tool_decision = route_decision.get("tool_decision") if isinstance(route_decision.get("tool_decision"), dict) else {}
    tool_decision_plan = tool_decision.get("action_plan") if isinstance(tool_decision.get("action_plan"), list) else []
    for item in [*action_plan, *tool_decision_plan]:
        if isinstance(item, dict) and str(item.get("tool") or "").strip().lower() == wanted:
            return True
    return bool(route_decision.get("planned_delegate")) and wanted == "delegate_crawler"


def _base_route_executor_metadata(
    *,
    adapter: str,
    migration_status: str,
    route_label: str,
    agent_id: str,
    graph_name: str,
    node_name: str,
    runtime_request: dict[str, Any] | None = None,
    route_decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    runtime_request = runtime_request if isinstance(runtime_request, dict) else {}
    route_decision = route_decision if isinstance(route_decision, dict) else {}
    display_agent = _display_agent(agent_id)
    return {
        "adapter": adapter,
        "agent_id": agent_id,
        "graph": graph_name,
        "node": node_name,
        "runtime_request_id": runtime_request.get("request_id") or "",
        "contract_kind": runtime_request.get("contract_kind") or "",
        "session_id": runtime_request.get("session_id") or "",
        "route_decision_id": route_decision.get("route_decision_id") or "",
        "route_intent": route_decision.get("route_intent") or "",
        "migration_status": migration_status,
        "decision_owner": f"{display_agent} LLM",
        "route_decision_executed_by_graph": bool(route_decision.get("routed")),
        "legacy_runtime_adapter_bypassed": True,
        "side_effect_executed": False,
        "objective_boundary": (
            f"The graph executed only the already Agent-selected, side-effect-free {route_label} route. "
            "It did not choose a tool, start jobs, judge evidence, persist data, or alter AgentMessage routing."
        ),
    }


def graph_status_route_executor_metadata(
    *,
    agent_id: str,
    graph_name: str,
    node_name: str,
    runtime_request: dict[str, Any] | None = None,
    route_decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _base_route_executor_metadata(
        adapter=GRAPH_STATUS_ROUTE_EXECUTOR,
        migration_status="graph_status_route_migrated",
        route_label="status",
        agent_id=agent_id,
        graph_name=graph_name,
        node_name=node_name,
        runtime_request=runtime_request,
        route_decision=route_decision,
    )


def graph_crawler_audit_route_executor_metadata(
    *,
    agent_id: str,
    graph_name: str,
    node_name: str,
    runtime_request: dict[str, Any] | None = None,
    route_decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _base_route_executor_metadata(
        adapter=GRAPH_CRAWLER_AUDIT_ROUTE_EXECUTOR,
        migration_status="graph_crawler_audit_route_migrated",
        route_label="crawler_audit",
        agent_id=agent_id,
        graph_name=graph_name,
        node_name=node_name,
        runtime_request=runtime_request,
        route_decision=route_decision,
    )


def graph_local_corpus_inventory_route_executor_metadata(
    *,
    agent_id: str,
    graph_name: str,
    node_name: str,
    runtime_request: dict[str, Any] | None = None,
    route_decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _base_route_executor_metadata(
        adapter=GRAPH_LOCAL_CORPUS_INVENTORY_ROUTE_EXECUTOR,
        migration_status="graph_local_corpus_inventory_route_migrated",
        route_label="local_corpus_inventory",
        agent_id=agent_id,
        graph_name=graph_name,
        node_name=node_name,
        runtime_request=runtime_request,
        route_decision=route_decision,
    )
