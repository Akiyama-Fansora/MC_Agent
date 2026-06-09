from __future__ import annotations

from typing import Any


LEGACY_HANDLER_SURFACES = [
    {
        "surface": "router_error",
        "legacy_handler": "AgentToolExecutor.router_error",
        "trace_stage": "done",
        "trace_statuses": ["router_error"],
        "side_effect_surface": False,
    },
    {
        "surface": "direct_answer",
        "legacy_handler": "_handle_direct_answer_route",
        "trace_stage": "answer",
        "trace_statuses": ["generating"],
        "side_effect_surface": False,
    },
    {
        "surface": "crawler_audit",
        "legacy_handler": "_handle_crawler_audit_route",
        "trace_stage": "audit",
        "trace_statuses": ["next_step_confirmed", "recent_crawler_audit", "recent_crawler_audit_missing"],
        "side_effect_surface": False,
    },
    {
        "surface": "status",
        "legacy_handler": "_handle_status_route",
        "trace_stage": "status",
        "trace_statuses": ["next_step_confirmed"],
        "side_effect_surface": False,
    },
    {
        "surface": "temporary_extract",
        "legacy_handler": "_handle_temporary_extract_route",
        "trace_stage": "extract",
        "trace_statuses": ["next_step_confirmed", "temporary_url_selected", "temporary_url_extracted", "temporary_url_failed"],
        "side_effect_surface": False,
    },
    {
        "surface": "local_corpus_inventory",
        "legacy_handler": "_handle_local_corpus_inventory_route",
        "trace_stage": "retrieve",
        "trace_statuses": ["inventory_next_step_confirmed", "inventory_scanning", "inventory_done"],
        "side_effect_surface": False,
    },
    {
        "surface": "mcagent_inventory_planned_workflow",
        "legacy_handler": "_handle_mcagent_inventory_planned_workflow_route",
        "trace_stage": "plan",
        "trace_statuses": ["executing_agent_selected_step"],
        "side_effect_surface": True,
    },
    {
        "surface": "delegate_crawler",
        "legacy_handler": "_handle_delegate_crawler_route",
        "trace_stage": "delegate",
        "trace_statuses": ["next_step_confirmed", "planned_workflow"],
        "side_effect_surface": True,
    },
    {
        "surface": "crawler_action_plan_delegate",
        "legacy_handler": "_handle_crawler_action_plan_delegate_route",
        "trace_stage": "plan",
        "trace_statuses": ["executing_agent_selected_step", "mcagent_context_completed"],
        "side_effect_surface": True,
    },
    {
        "surface": "no_retrieval_results",
        "legacy_handler": "_handle_no_retrieval_results",
        "trace_stage": "done",
        "trace_statuses": ["insufficient_evidence"],
        "side_effect_surface": False,
    },
    {
        "surface": "rag_answer_generation",
        "legacy_handler": "_handle_rag_answer_generation_route",
        "trace_stage": "answer",
        "trace_statuses": ["next_step_confirmed", "generating", "local_fact_answer"],
        "side_effect_surface": False,
    },
]
MIGRATED_GRAPH_HANDLER_SURFACES = {"status", "crawler_audit", "local_corpus_inventory", "router_error"}


def _surface_candidates_for_agent(agent_id: str) -> list[dict[str, Any]]:
    if agent_id == "crawler_agent":
        excluded = {"mcagent_inventory_planned_workflow"}
    else:
        excluded = {"crawler_action_plan_delegate"}
    return [dict(item) for item in LEGACY_HANDLER_SURFACES if item["surface"] not in excluded]


def _observed_statuses(route_execution_contract: dict[str, Any]) -> dict[str, list[str]]:
    trace_facts = route_execution_contract.get("trace_facts") if isinstance(route_execution_contract.get("trace_facts"), dict) else {}
    return {
        "answer": [str(item) for item in trace_facts.get("answer_statuses") or []],
        "retrieve": [str(item) for item in trace_facts.get("retrieve_statuses") or []],
        "delegate": [str(item) for item in trace_facts.get("delegate_statuses") or []],
        "extract": [str(item) for item in trace_facts.get("extract_statuses") or []],
        "status": [str(item) for item in trace_facts.get("status_statuses") or []],
        "audit": [str(item) for item in trace_facts.get("audit_statuses") or []],
        "done": [str(item) for item in trace_facts.get("done_statuses") or []],
    }


def _observed_surfaces(candidates: list[dict[str, Any]], statuses_by_stage: dict[str, list[str]]) -> list[str]:
    observed: list[str] = []
    for item in candidates:
        stage = str(item.get("trace_stage") or "")
        observed_statuses = set(statuses_by_stage.get(stage) or [])
        candidate_statuses = {str(status) for status in item.get("trace_statuses") or []}
        if observed_statuses & candidate_statuses:
            observed.append(str(item.get("surface") or ""))
    return observed


def build_legacy_handler_surface_contract(
    *,
    thread_id: str,
    graph_name: str,
    agent_id: str,
    node_name: str,
    contract_kind: str,
    decision_owner: str,
    runtime_request: dict[str, Any],
    runtime_adapter: dict[str, Any],
    route_decision_output_contract: dict[str, Any],
    route_execution_contract: dict[str, Any],
) -> dict[str, Any]:
    """Record legacy handler surface facts without choosing or executing a handler."""

    candidates = _surface_candidates_for_agent(agent_id)
    statuses_by_stage = _observed_statuses(route_execution_contract)
    observed_surfaces = _observed_surfaces(candidates, statuses_by_stage)
    graph_handler_executed = bool(route_execution_contract.get("route_execution_executed_by_graph")) and bool(
        MIGRATED_GRAPH_HANDLER_SURFACES & set(observed_surfaces)
    )
    side_effect_surfaces = [str(item["surface"]) for item in candidates if item.get("side_effect_surface")]
    return {
        "contract_id": f"{thread_id}:{agent_id}:legacy_handler_surface",
        "node": node_name,
        "graph": graph_name,
        "agent_id": agent_id,
        "session_id": str(runtime_request.get("session_id") or thread_id),
        "contract_kind": contract_kind,
        "runtime_request_id": runtime_request.get("request_id") or runtime_adapter.get("runtime_request_id") or "",
        "route_decision_output_contract_id": route_decision_output_contract.get("contract_id") or "",
        "route_execution_contract_id": route_execution_contract.get("contract_id") or "",
        "legacy_adapter_id": runtime_adapter.get("adapter") or "",
        "candidate_handler_surfaces": candidates,
        "candidate_surface_count": len(candidates),
        "candidate_side_effect_surfaces": side_effect_surfaces,
        "observed_trace_statuses_by_stage": statuses_by_stage,
        "observed_surface_signals": observed_surfaces,
        "observed_surface_signal_count": len(observed_surfaces),
        "decision_owner": decision_owner,
        "handler_selection_executed_by_graph": False,
        "handler_executed_by_contract": graph_handler_executed,
        "side_effect_executed_by_contract": False,
        "legacy_handlers_still_run_in_adapter": not graph_handler_executed,
        "legacy_trace_observation_only": not graph_handler_executed,
        "objective_contract": (
            "The graph records handler surface facts. It does not select handlers, start jobs, persist evidence, "
            "judge evidence, or alter routing. Migrated status, crawler_audit, router_error, and safe local_corpus_inventory "
            "routes may be observed as graph-executed only after the Agent router selected that route."
        ),
    }
