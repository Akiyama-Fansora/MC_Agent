from __future__ import annotations

from typing import Any


EXECUTION_TRACE_STAGES = {"retrieve", "answer", "delegate", "extract", "status", "audit", "done"}
GRAPH_ROUTE_EXECUTORS = {"graph_status_route_executor", "graph_crawler_audit_route_executor"}


def _trace_steps(result: dict[str, Any]) -> list[dict[str, Any]]:
    trace = result.get("trace") if isinstance(result.get("trace"), list) else []
    return [dict(step) for step in trace if isinstance(step, dict)]


def _statuses_for(trace: list[dict[str, Any]], stage: str) -> list[str]:
    return [str(step.get("status") or "") for step in trace if step.get("stage") == stage]


def _has(trace: list[dict[str, Any]], stage: str, status: str) -> bool:
    return any(step.get("stage") == stage and step.get("status") == status for step in trace)


def build_route_execution_contract(
    *,
    thread_id: str,
    graph_name: str,
    agent_id: str,
    node_name: str,
    contract_kind: str,
    decision_owner: str,
    result: dict[str, Any],
    runtime_request: dict[str, Any],
    runtime_adapter: dict[str, Any],
    route_input_contract: dict[str, Any],
    route_decision_output_contract: dict[str, Any],
    message_preflight_contract: dict[str, Any],
    contextual_question_contract: dict[str, Any] | None = None,
    source_planning_contract: dict[str, Any] | None = None,
    side_effect_authorization_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record route execution facts already produced by the Agent runtime."""

    trace = _trace_steps(result)
    graph_executor = result.get("graph_route_executor") if isinstance(result.get("graph_route_executor"), dict) else {}
    graph_executor_adapter = str(graph_executor.get("adapter") or "")
    graph_route_executed = graph_executor_adapter in GRAPH_ROUTE_EXECUTORS
    execution_stages = sorted({str(step.get("stage") or "") for step in trace if step.get("stage") in EXECUTION_TRACE_STAGES})
    sources = result.get("sources") if isinstance(result.get("sources"), list) else []
    answer = str(result.get("answer") or "")
    context = str(result.get("context") or "")
    job_data = result.get("job") if isinstance(result.get("job"), dict) else {}
    delegation = result.get("delegation") if isinstance(result.get("delegation"), dict) else {}
    job_id_keys = [
        key
        for key in ("job_id", "crawler_job_id", "background_job_id")
        if str(result.get(key) or "").strip()
    ]
    if str(job_data.get("id") or "").strip():
        job_id_keys.append("job.id")
    contextual_question_contract = contextual_question_contract or {}
    source_planning_contract = source_planning_contract or {}
    side_effect_authorization_contract = side_effect_authorization_contract or {}
    return {
        "contract_id": f"{thread_id}:{agent_id}:route_execution",
        "node": node_name,
        "graph": graph_name,
        "agent_id": agent_id,
        "session_id": str(result.get("session_id") or runtime_request.get("session_id") or thread_id),
        "contract_kind": contract_kind,
        "runtime_request_id": runtime_request.get("request_id") or runtime_adapter.get("runtime_request_id") or "",
        "route_input_contract_id": route_input_contract.get("contract_id") or runtime_request.get("route_input_contract_id") or "",
        "route_decision_output_contract_id": route_decision_output_contract.get("contract_id") or "",
        "message_preflight_contract_id": message_preflight_contract.get("contract_id") or runtime_request.get("message_preflight_contract_id") or "",
        "contextual_question_contract_id": contextual_question_contract.get("contract_id") or runtime_request.get("contextual_question_contract_id") or "",
        "source_planning_contract_id": source_planning_contract.get("contract_id") or runtime_request.get("source_planning_contract_id") or "",
        "side_effect_authorization_contract_id": side_effect_authorization_contract.get("contract_id") or runtime_request.get("side_effect_authorization_contract_id") or "",
        "legacy_adapter_id": runtime_adapter.get("adapter") or "",
        "trace_facts": {
            "trace_count": len(trace),
            "observed_execution_stages": execution_stages,
            "answer_statuses": _statuses_for(trace, "answer"),
            "retrieve_statuses": _statuses_for(trace, "retrieve"),
            "delegate_statuses": _statuses_for(trace, "delegate"),
            "extract_statuses": _statuses_for(trace, "extract"),
            "status_statuses": _statuses_for(trace, "status"),
            "audit_statuses": _statuses_for(trace, "audit"),
            "done_statuses": _statuses_for(trace, "done"),
            "has_answer_generation_trace": _has(trace, "answer", "generating"),
            "has_retrieval_trace": any(step.get("stage") == "retrieve" for step in trace),
            "has_delegate_trace": any(step.get("stage") == "delegate" for step in trace),
            "has_extract_trace": any(step.get("stage") == "extract" for step in trace),
            "has_status_trace": any(step.get("stage") == "status" for step in trace),
            "has_audit_trace": any(step.get("stage") == "audit" for step in trace),
            "has_response_ready_trace": _has(trace, "done", "response_ready"),
            "has_insufficient_evidence_trace": _has(trace, "done", "insufficient_evidence"),
            "has_router_error_trace": _has(trace, "done", "router_error"),
        },
        "result_facts": {
            "result_keys": sorted(str(key) for key in result.keys()),
            "agent": str(result.get("agent") or ""),
            "answer_present": bool(answer.strip()),
            "answer_length": len(answer),
            "context_present": bool(context.strip()),
            "context_length": len(context),
            "source_count": len(sources),
            "job_present": bool(job_data),
            "job_id_present": bool(job_id_keys),
            "job_id_keys": sorted(set(job_id_keys)),
            "delegation_present": bool(delegation),
            "collaboration_present": isinstance(result.get("collaboration"), list),
            "temporary_extract_present": isinstance(result.get("temporary_extract"), dict),
            "agent_message_present": isinstance(result.get("agent_message"), dict),
            "error_present": bool(result.get("error")),
        },
        "decision_owner": decision_owner,
        "route_execution_executed_by_graph": graph_route_executed,
        "side_effect_executed_by_contract": False,
        "response_changed_by_contract": False,
        "legacy_execution_still_runs_in_adapter": runtime_adapter.get("adapter") == "legacy_web_server_runtime",
        "legacy_trace_observation_only": not graph_route_executed,
        "objective_contract": (
            "The graph records Agent route execution facts. For migrated status and crawler_audit routes, the graph "
            "may execute only the already-selected side-effect-free handler; this contract does not start jobs, "
            "persist evidence, judge evidence, alter routing, or write the final response."
        ),
    }
