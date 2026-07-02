from __future__ import annotations

from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.rag_service import RagRetrievalPreparation, RagRetrievalService  # noqa: E402
from mcagent.retrieval_planner import RetrievalPlan  # noqa: E402
from mcagent.schema import SearchResult  # noqa: E402


def make_result(rank: int, title: str = "Evidence") -> SearchResult:
    return SearchResult(
        rank=rank,
        score=10.0 - rank,
        chunk_id=rank,
        document_id=rank,
        chunk_index=0,
        title=f"{title} {rank}",
        source_path=f"source-{rank}.md",
        url=f"https://example.test/{rank}",
        text=f"body {rank}",
        metadata={"source": "test"},
    )


class FakeRetriever:
    def __init__(self, results: list[SearchResult], calls: list[dict[str, Any]]) -> None:
        self._results = results
        self._calls = calls

    def search(self, query: str, *, top_k: int, plan: RetrievalPlan | None = None, session_summary: dict[str, Any] | None = None):
        self._calls.append({"query": query, "top_k": top_k, "plan": plan, "session_summary": session_summary})
        return list(self._results)


def assert_equal(name: str, actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"{name}: expected {expected!r}, got {actual!r}")


def assert_true(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name}: {detail}")


def build_service(
    *,
    retriever_results: list[SearchResult],
    calls: list[dict[str, Any]],
    plan_calls: list[dict[str, Any]],
    supplement_calls: list[dict[str, Any]],
) -> RagRetrievalService:
    def plan_fn(question: str, *, session_summary: dict[str, Any], max_queries: int, use_llm: bool) -> RetrievalPlan:
        plan_calls.append(
            {
                "question": question,
                "session_summary": session_summary,
                "max_queries": max_queries,
                "use_llm": use_llm,
            }
        )
        return RetrievalPlan(topic=question, subqueries=[f"{question} guide"], required_terms=[question], planner="test")

    def supplement(_config: object, query: str, results: list[SearchResult], limit: int) -> list[SearchResult]:
        supplement_calls.append({"query": query, "results": len(results), "limit": limit})
        return [*results, make_result(99, "Raw HTML")]

    return RagRetrievalService(
        retriever_factory=lambda _config: FakeRetriever(retriever_results, calls),
        plan_fn=plan_fn,
        adaptive_rough_k=lambda query, agent: 42 if agent == "mcagent_rag" and query else 12,
        adaptive_final_k=lambda query, _config, agent: 8 if agent == "mcagent_rag" and query else 4,
        planning_summary=lambda summary, original, evidence: {"summary": summary, "original": original, "evidence": evidence},
        combined_question=lambda evidence, question, plan: f"{evidence} :: {question} :: {plan.topic if plan else 'no-plan'}",
        supplement_results=supplement,
        dedupe_results=lambda results, limit: list(results)[:limit],
    )


def test_prepare_prefers_rag_focus_and_adaptive_limits() -> None:
    service = build_service(retriever_results=[], calls=[], plan_calls=[], supplement_calls=[])
    preparation = service.prepare(object(), agent="mcagent_rag", question="raw user question", rag_focus="focused topic")
    assert_equal("evidence_question", preparation.evidence_question, "focused topic")
    assert_equal("rough_k", preparation.rough_k, 42)
    assert_equal("final_k", preparation.final_k, 8)


def test_prepare_ignores_blank_rag_focus() -> None:
    service = build_service(retriever_results=[], calls=[], plan_calls=[], supplement_calls=[])
    preparation = service.prepare(object(), agent="mcagent_rag", question="raw user question", rag_focus="  \r\n\t  ")
    assert_equal("evidence_question", preparation.evidence_question, "raw user question")
    assert_equal("rough_k", preparation.rough_k, 42)
    assert_equal("final_k", preparation.final_k, 8)


def test_planned_retrieval_emits_planning_and_searches_combined_query() -> None:
    calls: list[dict[str, Any]] = []
    plan_calls: list[dict[str, Any]] = []
    supplement_calls: list[dict[str, Any]] = []
    traces: list[dict[str, Any]] = []
    service = build_service(
        retriever_results=[make_result(1), make_result(2), make_result(3), make_result(4)],
        calls=calls,
        plan_calls=plan_calls,
        supplement_calls=supplement_calls,
    )

    result = service.retrieve(
        object(),
        agent="mcagent_rag",
        original_question="original",
        question="contextualized",
        session_summary={"topic": "utopia"},
        preparation=RagRetrievalPreparation(evidence_question="utopia guide", rough_k=42, final_k=8),
        use_planner=True,
        add_trace=lambda stage, status, detail=None: traces.append({"stage": stage, "status": status, "detail": detail}) or traces[-1],
    )

    assert_equal("plan_question", plan_calls[0]["question"], "utopia guide")
    assert_equal("plan_summary_original", plan_calls[0]["session_summary"]["original"], "original")
    assert_equal("search_query", calls[0]["query"], "utopia guide :: contextualized :: utopia guide")
    assert_equal("search_top_k", calls[0]["top_k"], 42)
    assert_true("retrieval_plan", isinstance(result.retrieval_plan, RetrievalPlan))
    assert_equal("selected_count", len(result.selected), 4)
    assert_equal("trace_statuses", [item["status"] for item in traces], ["planning", "planned", "searching", "done"])
    assert_equal("no_supplement", len(supplement_calls), 0)


def test_sparse_mcagent_results_are_supplemented_without_deciding_final_answer() -> None:
    calls: list[dict[str, Any]] = []
    supplement_calls: list[dict[str, Any]] = []
    service = build_service(
        retriever_results=[make_result(1)],
        calls=calls,
        plan_calls=[],
        supplement_calls=supplement_calls,
    )
    traces: list[dict[str, Any]] = []

    result = service.retrieve(
        object(),
        agent="mcagent_rag",
        original_question="original",
        question="question",
        session_summary={},
        preparation=RagRetrievalPreparation(evidence_question="topic", rough_k=12, final_k=8),
        use_planner=False,
        add_trace=lambda stage, status, detail=None: traces.append({"stage": stage, "status": status, "detail": detail}) or traces[-1],
    )

    assert_equal("supplement_query", supplement_calls[0]["query"], "topic")
    assert_equal("supplement_limit", supplement_calls[0]["limit"], 24)
    assert_equal("rough_after_supplement", len(result.rough_results), 2)
    assert_equal("selected_after_supplement", len(result.selected), 2)
    assert_true("no_evidence_verdict_trace", all(item["status"] != "evidence_selected" for item in traces))


def test_non_mcagent_retrieval_does_not_plan_or_supplement() -> None:
    calls: list[dict[str, Any]] = []
    plan_calls: list[dict[str, Any]] = []
    supplement_calls: list[dict[str, Any]] = []
    traces: list[dict[str, Any]] = []
    service = build_service(
        retriever_results=[make_result(1)],
        calls=calls,
        plan_calls=plan_calls,
        supplement_calls=supplement_calls,
    )

    result = service.retrieve(
        object(),
        agent="retriever_only",
        original_question="original",
        question="question",
        session_summary={},
        preparation=RagRetrievalPreparation(evidence_question="topic", rough_k=5, final_k=4),
        use_planner=False,
        add_trace=lambda stage, status, detail=None: traces.append({"stage": stage, "status": status, "detail": detail}) or traces[-1],
    )

    assert_equal("plan_calls", len(plan_calls), 0)
    assert_equal("supplement_calls", len(supplement_calls), 0)
    assert_equal("search_query", calls[0]["query"], "topic :: question :: no-plan")
    assert_equal("selected_count", len(result.selected), 1)
    assert_equal("trace_statuses", [item["status"] for item in traces], ["searching", "done"])


if __name__ == "__main__":
    test_prepare_prefers_rag_focus_and_adaptive_limits()
    test_prepare_ignores_blank_rag_focus()
    test_planned_retrieval_emits_planning_and_searches_combined_query()
    test_sparse_mcagent_results_are_supplemented_without_deciding_final_answer()
    test_non_mcagent_retrieval_does_not_plan_or_supplement()
    print("rag_service_scenarios: ok")
