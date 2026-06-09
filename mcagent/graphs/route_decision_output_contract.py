from __future__ import annotations

from typing import Any


def _trace_steps(result: dict[str, Any]) -> list[dict[str, Any]]:
    trace = result.get("trace") if isinstance(result.get("trace"), list) else []
    return [dict(step) for step in trace if isinstance(step, dict)]


def _detail(step: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(step, dict):
        return {}
    detail = step.get("detail")
    return detail if isinstance(detail, dict) else {}


def _steps_with(trace: list[dict[str, Any]], *, stage: str, status: str) -> list[dict[str, Any]]:
    return [step for step in trace if step.get("stage") == stage and step.get("status") == status]


def _plan_tools(steps: list[Any]) -> list[str]:
    tools: list[str] = []
    for item in steps:
        if isinstance(item, dict):
            tool = str(item.get("tool") or "").strip()
            if tool:
                tools.append(tool)
    return tools


def build_route_decision_output_contract(
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
) -> dict[str, Any]:
    """Record route-output facts already emitted by the legacy runtime trace."""

    trace = _trace_steps(result)
    tool_selected_steps = _steps_with(trace, stage="decide", status="tool_selected")
    confirmation_steps = _steps_with(trace, stage="decide", status="next_step_confirmed")
    plan_created_steps = _steps_with(trace, stage="plan", status="created")
    plan_replacement_steps = _steps_with(trace, stage="plan", status="confirmed_replacement")
    selected_detail = _detail(tool_selected_steps[-1] if tool_selected_steps else None)
    selected_decision = selected_detail.get("decision") if isinstance(selected_detail.get("decision"), dict) else {}
    confirmation_detail = _detail(confirmation_steps[-1] if confirmation_steps else None)
    created_plan_detail = _detail(plan_created_steps[-1] if plan_created_steps else None)
    replacement_plan_detail = _detail(plan_replacement_steps[-1] if plan_replacement_steps else None)
    selected_plan = selected_decision.get("action_plan") if isinstance(selected_decision.get("action_plan"), list) else []
    created_plan = created_plan_detail.get("steps") if isinstance(created_plan_detail.get("steps"), list) else []
    replacement_plan = replacement_plan_detail.get("steps") if isinstance(replacement_plan_detail.get("steps"), list) else []
    observed_plan = replacement_plan or created_plan or selected_plan
    contextual_question_contract = contextual_question_contract or {}
    source_planning_contract = source_planning_contract or {}
    side_effect_authorization_contract = side_effect_authorization_contract or {}
    job_id_keys = [
        key
        for key in ("job_id", "crawler_job_id", "background_job_id")
        if str(result.get(key) or "").strip()
    ]
    return {
        "contract_id": f"{thread_id}:{agent_id}:route_decision_output",
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
        "legacy_adapter_id": runtime_adapter.get("adapter") or "",
        "trace_facts": {
            "trace_count": len(trace),
            "has_tool_selected_trace": bool(tool_selected_steps),
            "tool_selected_trace_count": len(tool_selected_steps),
            "observed_selected_tool": str(selected_detail.get("tool") or selected_decision.get("tool") or ""),
            "observed_tool_decision_keys": sorted(str(key) for key in selected_decision.keys()),
            "has_next_step_confirmation_trace": bool(confirmation_steps),
            "next_step_confirmation_trace_count": len(confirmation_steps),
            "observed_confirmation_tool": str(confirmation_detail.get("tool") or ""),
            "observed_confirmation_suggested_tool": str(confirmation_detail.get("suggested_tool") or ""),
            "observed_confirmation_proceed_value": confirmation_detail.get("proceed") if "proceed" in confirmation_detail else None,
            "observed_confirmation_reused_agent_decision": bool(confirmation_detail.get("reused_agent_decision")),
            "observed_confirmation_planner": str(confirmation_detail.get("planner") or ""),
            "has_plan_created_trace": bool(plan_created_steps),
            "has_plan_replacement_trace": bool(plan_replacement_steps),
            "observed_plan_step_count": len(observed_plan),
            "observed_plan_tools": _plan_tools(observed_plan),
            "result_job_id_present": bool(job_id_keys),
            "result_job_id_keys": job_id_keys,
        },
        "decision_owner": decision_owner,
        "route_decision_executed_by_graph": False,
        "route_confirmation_executed_by_graph": False,
        "route_execution_changed_by_contract": False,
        "legacy_route_still_runs_in_adapter": True,
        "legacy_trace_observation_only": True,
        "objective_contract": (
            "The graph records legacy route-output trace facts only. It does not choose tools, "
            "confirm next steps, alter routing, start jobs, judge evidence, or write the final response."
        ),
    }
