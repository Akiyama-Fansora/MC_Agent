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


def build_crawler_job_graph(config: AppConfig, legacy_loop: CrawlerJobLoopFn, job: Any):
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
                "CrawlerAgent still owns planning, observation review, acceptance, retry, and final reporting."
            ),
        }
        event_update = _append(
            state,
            "crawler_job.prepare",
            "legacy_loop_contract_exposed",
            contract,
        )
        return {
            **event_update,
            "job_contract": contract,
        }

    def run_legacy_loop(state: CrawlerJobGraphState) -> dict[str, Any]:
        legacy_loop(job, dict(state.get("payload") or {}), config)
        return _append(state, "crawler_job.legacy_loop", "completed", {"job_id": state.get("job_id") or ""})

    def finalize(state: CrawlerJobGraphState) -> dict[str, Any]:
        graph_runtime = {
            "runtime": "langgraph",
            "graph": "CrawlerJobGraph",
            "job_id": state.get("job_id") or "",
            "job_contract": state.get("job_contract") or {},
            "visited_nodes": [*state.get("visited_nodes", []), "crawler_job.finalize"],
            "events": [*state.get("graph_events", []), _event("crawler_job.finalize", "ready")],
        }
        current_result = job.result if isinstance(getattr(job, "result", None), dict) else {}
        job.result = {**current_result, "crawler_job_graph_runtime": graph_runtime}
        return {
            "visited_nodes": graph_runtime["visited_nodes"],
            "graph_events": graph_runtime["events"],
        }

    builder.add_node("receive", receive)
    builder.add_node("prepare", prepare)
    builder.add_node("legacy_loop", run_legacy_loop)
    builder.add_node("finalize", finalize)
    builder.add_edge(START, "receive")
    builder.add_edge("receive", "prepare")
    builder.add_edge("prepare", "legacy_loop")
    builder.add_edge("legacy_loop", "finalize")
    builder.add_edge("finalize", END)
    return builder.compile()


def run_crawler_job_graph(config: AppConfig, job: Any, payload: dict[str, Any], *, legacy_loop: CrawlerJobLoopFn) -> None:
    graph = build_crawler_job_graph(config, legacy_loop, job)
    graph.invoke(
        {
            "job_id": str(getattr(job, "id", "") or ""),
            "payload": dict(payload),
            "job_contract": {},
            "graph_events": [],
            "visited_nodes": [],
            "errors": [],
        }
    )
