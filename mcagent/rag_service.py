from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .config import AppConfig
from .retrieval_planner import RetrievalPlan, plan_retrieval
from .retriever import Retriever
from .schema import SearchResult


class TraceFn(Protocol):
    def __call__(self, stage: str, status: str, detail: Any = None) -> dict[str, Any]:
        ...


RetrieverFactory = Callable[[AppConfig], Retriever]
AdaptiveRoughKFn = Callable[[str, str], int]
AdaptiveFinalKFn = Callable[[str, AppConfig, str], int]
PlanningSummaryFn = Callable[[dict[str, Any], str, str], dict[str, Any]]
CombinedQuestionFn = Callable[[str, str, Any | None], str]
SupplementResultsFn = Callable[[AppConfig, str, list[SearchResult], int], list[SearchResult]]
DedupeResultsFn = Callable[[list[SearchResult], int], list[SearchResult]]


@dataclass(slots=True)
class RagRetrievalPreparation:
    evidence_question: str
    rough_k: int
    final_k: int


@dataclass(slots=True)
class RagRetrievalResult:
    evidence_question: str
    rough_k: int
    final_k: int
    retrieval_plan: RetrievalPlan | None
    search_question: str
    rough_results: list[SearchResult]
    selected: list[SearchResult]


class RagRetrievalService:
    """Execute local RAG retrieval after the Agent has chosen to use it."""

    def __init__(
        self,
        *,
        retriever_factory: RetrieverFactory = Retriever,
        plan_fn: Callable[..., RetrievalPlan] = plan_retrieval,
        adaptive_rough_k: AdaptiveRoughKFn,
        adaptive_final_k: AdaptiveFinalKFn,
        planning_summary: PlanningSummaryFn,
        combined_question: CombinedQuestionFn,
        supplement_results: SupplementResultsFn,
        dedupe_results: DedupeResultsFn,
    ) -> None:
        self._retriever_factory = retriever_factory
        self._plan_fn = plan_fn
        self._adaptive_rough_k = adaptive_rough_k
        self._adaptive_final_k = adaptive_final_k
        self._planning_summary = planning_summary
        self._combined_question = combined_question
        self._supplement_results = supplement_results
        self._dedupe_results = dedupe_results

    def prepare(self, config: AppConfig, *, agent: str, question: str, rag_focus: str = "") -> RagRetrievalPreparation:
        evidence_question = str(rag_focus or "").strip() or question
        return RagRetrievalPreparation(
            evidence_question=evidence_question,
            rough_k=self._adaptive_rough_k(evidence_question, agent),
            final_k=self._adaptive_final_k(evidence_question, config, agent),
        )

    def retrieve(
        self,
        config: AppConfig,
        *,
        agent: str,
        original_question: str,
        question: str,
        session_summary: dict[str, Any],
        preparation: RagRetrievalPreparation,
        use_planner: bool,
        add_trace: TraceFn,
    ) -> RagRetrievalResult:
        retrieval_plan: RetrievalPlan | None = None
        if use_planner:
            summary = self._planning_summary(session_summary, original_question, preparation.evidence_question)
            add_trace("retrieve", "planning", {"question": preparation.evidence_question, "original": original_question})
            retrieval_plan = self._plan_fn(
                preparation.evidence_question,
                session_summary=summary,
                max_queries=10,
                use_llm=True,
            )
            add_trace("retrieve", "planned", retrieval_plan.to_dict())

        add_trace(
            "retrieve",
            "searching",
            {
                "mode": "planned_adaptive" if retrieval_plan else "adaptive",
                "rough_k": preparation.rough_k,
                "final_context_k": preparation.final_k,
            },
        )
        search_question = self._combined_question(preparation.evidence_question, question, retrieval_plan)
        retriever = self._retriever_factory(config)
        rough_results = retriever.search(
            search_question,
            top_k=preparation.rough_k,
            plan=retrieval_plan,
            session_summary=session_summary,
        )
        if agent == "mcagent_rag" and len(rough_results) < max(4, preparation.final_k // 2):
            rough_results = self._supplement_results(config, preparation.evidence_question, rough_results, 24)

        add_trace(
            "retrieve",
            "done",
            {
                "results": len(rough_results),
                "top": rough_results[0].title if rough_results else "",
            },
        )
        selected = self._dedupe_results(rough_results, preparation.final_k)
        return RagRetrievalResult(
            evidence_question=preparation.evidence_question,
            rough_k=preparation.rough_k,
            final_k=preparation.final_k,
            retrieval_plan=retrieval_plan,
            search_question=search_question,
            rough_results=rough_results,
            selected=selected,
        )
