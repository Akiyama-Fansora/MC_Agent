from __future__ import annotations

from typing import Any


def build_route_result_contract(
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
    message_preflight_contract: dict[str, Any],
    contextual_question_contract: dict[str, Any] | None = None,
    source_planning_contract: dict[str, Any] | None = None,
    side_effect_authorization_contract: dict[str, Any] | None = None,
    route_decision_output_contract: dict[str, Any] | None = None,
    route_execution_contract: dict[str, Any] | None = None,
    legacy_handler_surface_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Describe the legacy result shape without judging or changing it."""

    metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    sources = result.get("sources") if isinstance(result.get("sources"), list) else []
    trace = result.get("trace") if isinstance(result.get("trace"), list) else []
    answer = str(result.get("answer") or "")
    context = str(result.get("context") or "")
    job_id_keys = [
        key
        for key in ("job_id", "crawler_job_id", "background_job_id")
        if str(result.get(key) or "").strip()
    ]
    contextual_question_contract = contextual_question_contract or {}
    source_planning_contract = source_planning_contract or {}
    side_effect_authorization_contract = side_effect_authorization_contract or {}
    route_decision_output_contract = route_decision_output_contract or {}
    route_execution_contract = route_execution_contract or {}
    legacy_handler_surface_contract = legacy_handler_surface_contract or {}
    return {
        "contract_id": f"{thread_id}:{agent_id}:route_result",
        "node": node_name,
        "graph": graph_name,
        "agent_id": agent_id,
        "session_id": str(result.get("session_id") or runtime_request.get("session_id") or thread_id),
        "contract_kind": contract_kind,
        "runtime_request_id": runtime_request.get("request_id") or runtime_adapter.get("runtime_request_id") or "",
        "route_input_contract_id": route_input_contract.get("contract_id") or runtime_request.get("route_input_contract_id") or "",
        "message_preflight_contract_id": message_preflight_contract.get("contract_id") or runtime_request.get("message_preflight_contract_id") or "",
        "contextual_question_contract_id": contextual_question_contract.get("contract_id") or runtime_request.get("contextual_question_contract_id") or "",
        "source_planning_contract_id": source_planning_contract.get("contract_id") or runtime_request.get("source_planning_contract_id") or "",
        "side_effect_authorization_contract_id": side_effect_authorization_contract.get("contract_id") or runtime_request.get("side_effect_authorization_contract_id") or "",
        "route_decision_output_contract_id": route_decision_output_contract.get("contract_id") or "",
        "route_execution_contract_id": route_execution_contract.get("contract_id") or "",
        "legacy_handler_surface_contract_id": legacy_handler_surface_contract.get("contract_id") or "",
        "legacy_adapter": {
            "adapter": runtime_adapter.get("adapter") or "",
            "node": runtime_adapter.get("node") or "",
            "runtime_request_id": runtime_adapter.get("runtime_request_id") or "",
        },
        "result_shape": {
            "result_keys": sorted(str(key) for key in result.keys()),
            "metadata_keys": sorted(str(key) for key in metadata.keys()),
            "agent": str(result.get("agent") or ""),
            "answer_present": bool(answer.strip()),
            "answer_length": len(answer),
            "context_present": bool(context.strip()),
            "context_length": len(context),
            "source_count": len(sources),
            "trace_count": len(trace),
            "has_agent_message": isinstance(result.get("agent_message"), dict),
            "job_id_present": bool(job_id_keys),
            "job_id_keys": job_id_keys,
            "error_present": bool(result.get("error")),
        },
        "decision_owner": decision_owner,
        "objective_contract": (
            "The graph records legacy result shape only. It does not select tools, judge evidence, "
            "start jobs, rewrite messages, or change the response returned by the legacy adapter."
        ),
    }
