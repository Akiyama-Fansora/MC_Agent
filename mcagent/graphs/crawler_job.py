from __future__ import annotations

from typing import Any, Callable, TypedDict

from langgraph.graph import END, START, StateGraph

from ..config import AppConfig
from .state import GraphEvent


CrawlerJobLoopFn = Callable[[Any, dict[str, Any], AppConfig], None]


class CrawlerJobGraphState(TypedDict):
    job_id: str
    payload: dict[str, Any]
    job_contract: dict[str, Any]
    job_phase_contract: dict[str, Any]
    objective_observations: dict[str, Any]
    graph_events: list[GraphEvent]
    visited_nodes: list[str]
    errors: list[dict[str, Any]]


def _event(node: str, status: str, detail: dict[str, Any] | None = None) -> GraphEvent:
    return {"node": node, "status": status, "detail": dict(detail or {})}


def _append(state: CrawlerJobGraphState, node: str, status: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "visited_nodes": [*state.get("visited_nodes", []), node],
        "graph_events": [*state.get("graph_events", []), _event(node, status, detail)],
    }


def _safe_job_result(job: Any) -> dict[str, Any]:
    result = getattr(job, "result", None)
    return dict(result) if isinstance(result, dict) else {}


def _safe_observation_count(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _task_observation_counts(tasks: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {
        "tasks_observed": 0,
        "accepted_or_reusable": 0,
        "empty": 0,
        "off_topic": 0,
        "failed": 0,
        "deferred_for_ingest": 0,
    }
    for item in tasks:
        if not isinstance(item, dict):
            continue
        counts["tasks_observed"] += 1
        returncode = _safe_observation_count(item.get("returncode") or 0)
        observation = item.get("observation") if isinstance(item.get("observation"), dict) else {}
        status = str(observation.get("status") or "")
        topic_validation = item.get("topic_validation") if isinstance(item.get("topic_validation"), dict) else {}
        accepted = bool(item.get("ingest_deferred")) or bool(item.get("existing_evidence_reused")) or bool(topic_validation.get("matched"))
        if accepted or status == "ok":
            counts["accepted_or_reusable"] += 1
        if bool(item.get("empty_result")) or status == "empty":
            counts["empty"] += 1
        if bool(item.get("off_topic_result")) or status == "off_topic":
            counts["off_topic"] += 1
        if returncode != 0 or status in {"error", "blocked"}:
            counts["failed"] += 1
        if item.get("ingest_deferred"):
            counts["deferred_for_ingest"] += 1
    return counts


def _objective_observations_from_job(job: Any) -> dict[str, Any]:
    result = _safe_job_result(job)
    tasks = list(result.get("tasks") or []) if isinstance(result.get("tasks"), list) else []
    planned = list(result.get("planned_tasks") or []) if isinstance(result.get("planned_tasks"), list) else []
    plan = result.get("plan") if isinstance(result.get("plan"), dict) else {}
    self_audit = result.get("self_audit") if isinstance(result.get("self_audit"), dict) else {}
    ingest = result.get("ingest") if isinstance(result.get("ingest"), dict) else None
    counts = _task_observation_counts(tasks)
    return {
        "job_id": str(getattr(job, "id", "") or ""),
        "job_status": str(getattr(job, "status", "") or ""),
        "planned_task_count": len(planned),
        "executed_task_count": len(tasks),
        "success_count": _safe_observation_count(result.get("success_count") or 0),
        "candidate_count": _safe_observation_count(result.get("candidate_count") or 0),
        "failure_count": _safe_observation_count(result.get("failure_count") or 0),
        "replan_count": _safe_observation_count(result.get("replan_count") or 0),
        "needs_ingest": bool(result.get("ingest_background")),
        "ingest_completed": bool(ingest),
        "agent_finish_reason": str(plan.get("agent_finish_reason") or ""),
        "observation_counts": counts,
        "self_audit_counts": self_audit.get("counts") if isinstance(self_audit.get("counts"), dict) else {},
        "accepted_sources_visible": counts["accepted_or_reusable"] > 0,
        "rejected_or_blocked_sources_visible": any(counts[key] > 0 for key in ("empty", "off_topic", "failed")),
        "objective_contract": (
            "CrawlerJobGraph observes the completed job loop facts: planned tasks, executed tasks, "
            "accepted/reused evidence, rejected/blocked results, ingest status, and self-audit counts. "
            "It records facts only; CrawlerAgent remains the decision owner."
        ),
    }


def build_crawler_job_graph(config: AppConfig, agent_loop: CrawlerJobLoopFn, job: Any):
    builder = StateGraph(CrawlerJobGraphState)

    def receive(state: CrawlerJobGraphState) -> dict[str, Any]:
        payload = dict(state.get("payload") or {})
        return {
            "payload": payload,
            **_append(
                state,
                "crawler_job.receive",
                "job_received",
                {
                    "job_id": state.get("job_id") or "",
                    "source": str(payload.get("source") or ""),
                    "session_id": str(payload.get("session_id") or ""),
                },
            ),
        }

    def prepare(state: CrawlerJobGraphState) -> dict[str, Any]:
        payload = dict(state.get("payload") or {})
        contract = {
            "source": str(payload.get("source") or ""),
            "delivery_target": str(payload.get("delivery_target") or ""),
            "requested_by": str(payload.get("requested_by") or ""),
            "has_agent_message": isinstance(payload.get("agent_message"), dict),
            "side_effects": ["network", "filesystem", "possible_rag_ingest"],
            "decision_owner": "CrawlerAgent LLM",
            "objective_contract": (
                "The job graph records side-effect boundaries and payload facts. "
                "CrawlerAgent owns planning, observation review, acceptance, retry, and final reporting."
            ),
            "graph_owned_job_phases": ["receive", "prepare", "phase_contract", "agent_loop", "observe_loop_result", "finalize"],
        }
        event_update = _append(
            state,
            "crawler_job.prepare",
            "agent_loop_contract_exposed",
            contract,
        )
        return {
            **event_update,
            "job_contract": contract,
        }

    def phase_contract(state: CrawlerJobGraphState) -> dict[str, Any]:
        payload = dict(state.get("payload") or {})
        contract = {
            "contract_kind": "crawler_job_phase_facts_contract",
            "job_id": state.get("job_id") or "",
            "source": str(payload.get("source") or ""),
            "decision_owner": "CrawlerAgent LLM",
            "tool_owner": "CrawlerAgent",
            "message_boundary": "AgentMessage(from_agent, content, to_agent)",
            "phase_facts_recorded_by_graph": [
                "plan_prepared",
                "task_action_observed",
                "source_acceptance_or_rejection_observed",
                "ingest_status_observed",
                "self_audit_observed",
            ],
            "graph_does_not_decide": [
                "which source to trust",
                "whether evidence is enough for MCagent",
                "whether to continue crawling",
                "how to summarize to the user",
            ],
        }
        return {
            **_append(state, "crawler_job.phase_contract", "phase_contract_recorded", contract),
            "job_phase_contract": contract,
        }

    def run_agent_loop(state: CrawlerJobGraphState) -> dict[str, Any]:
        agent_loop(job, dict(state.get("payload") or {}), config)
        return _append(state, "crawler_job.agent_loop", "completed", {"job_id": state.get("job_id") or ""})

    def observe_loop_result(state: CrawlerJobGraphState) -> dict[str, Any]:
        observations = _objective_observations_from_job(job)
        return {
            **_append(state, "crawler_job.observe_loop_result", "objective_loop_facts_recorded", observations),
            "objective_observations": observations,
        }

    def finalize(state: CrawlerJobGraphState) -> dict[str, Any]:
        graph_runtime = {
            "runtime": "langgraph",
            "graph": "CrawlerJobGraph",
            "job_id": state.get("job_id") or "",
            "job_contract": state.get("job_contract") or {},
            "job_phase_contract": state.get("job_phase_contract") or {},
            "objective_observations": state.get("objective_observations") or {},
            "visited_nodes": [*state.get("visited_nodes", []), "crawler_job.finalize"],
            "events": [*state.get("graph_events", []), _event("crawler_job.finalize", "ready")],
        }
        current_result = _safe_job_result(job)
        job.result = {**current_result, "crawler_job_graph_runtime": graph_runtime}
        return {
            "visited_nodes": graph_runtime["visited_nodes"],
            "graph_events": graph_runtime["events"],
        }

    builder.add_node("receive", receive)
    builder.add_node("prepare", prepare)
    builder.add_node("phase_contract", phase_contract)
    builder.add_node("agent_loop", run_agent_loop)
    builder.add_node("observe_loop_result", observe_loop_result)
    builder.add_node("finalize", finalize)
    builder.add_edge(START, "receive")
    builder.add_edge("receive", "prepare")
    builder.add_edge("prepare", "phase_contract")
    builder.add_edge("phase_contract", "agent_loop")
    builder.add_edge("agent_loop", "observe_loop_result")
    builder.add_edge("observe_loop_result", "finalize")
    builder.add_edge("finalize", END)
    return builder.compile()


def run_crawler_job_graph(config: AppConfig, job: Any, payload: dict[str, Any], *, agent_loop: CrawlerJobLoopFn) -> None:
    graph = build_crawler_job_graph(config, agent_loop, job)
    graph.invoke(
        {
            "job_id": str(getattr(job, "id", "") or ""),
            "payload": dict(payload),
            "job_contract": {},
            "job_phase_contract": {},
            "objective_observations": {},
            "graph_events": [],
            "visited_nodes": [],
            "errors": [],
        }
    )
