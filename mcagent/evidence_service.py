from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Callable, Protocol

from .config import AppConfig
from .evidence_selector import EvidenceReport, EvidenceSelector
from .retrieval_planner import RetrievalPlan
from .schema import SearchResult


class TraceFn(Protocol):
    def __call__(self, stage: str, status: str, detail: Any = None) -> dict[str, Any]:
        ...


SelectorFactory = Callable[[int], EvidenceSelector]
PreferParentTopicResultsFn = Callable[[str, list[SearchResult], list[SearchResult], int], list[SearchResult]]
ModpackManifestResultsFn = Callable[[str, list[SearchResult], int], list[SearchResult]]
SupplementLocalModpackManifestFn = Callable[[AppConfig, str, int], list[SearchResult]]
SupplementProjectKeywordResultsFn = Callable[[AppConfig, str, list[SearchResult], int], list[SearchResult]]
SupplementRawHtmlResultsFn = Callable[[AppConfig, str, list[SearchResult], int], list[SearchResult]]
EnsureModpackModListContextFn = Callable[[AppConfig, str, list[SearchResult], list[SearchResult], int], list[SearchResult]]
FallbackThemeResultsFn = Callable[[str, list[SearchResult], int], list[SearchResult]]
DedupeResultsFn = Callable[[list[SearchResult], int], list[SearchResult]]


@dataclass(slots=True)
class EvidenceWorkflowResult:
    selected: list[SearchResult]
    report: EvidenceReport


class EvidenceWorkflowService:
    """Run objective evidence selection after local retrieval has completed."""

    def __init__(
        self,
        *,
        selector_factory: SelectorFactory = EvidenceSelector,
        prefer_parent_topic_results: PreferParentTopicResultsFn,
        modpack_manifest_results: ModpackManifestResultsFn,
        supplement_local_modpack_manifest_results: SupplementLocalModpackManifestFn,
        supplement_project_keyword_results: SupplementProjectKeywordResultsFn,
        supplement_raw_html_results: SupplementRawHtmlResultsFn,
        ensure_modpack_mod_list_context: EnsureModpackModListContextFn,
        fallback_theme_results: FallbackThemeResultsFn,
        dedupe_results: DedupeResultsFn,
    ) -> None:
        self._selector_factory = selector_factory
        self._prefer_parent_topic_results = prefer_parent_topic_results
        self._modpack_manifest_results = modpack_manifest_results
        self._supplement_local_modpack_manifest_results = supplement_local_modpack_manifest_results
        self._supplement_project_keyword_results = supplement_project_keyword_results
        self._supplement_raw_html_results = supplement_raw_html_results
        self._ensure_modpack_mod_list_context = ensure_modpack_mod_list_context
        self._fallback_theme_results = fallback_theme_results
        self._dedupe_results = dedupe_results

    def select(
        self,
        config: AppConfig,
        *,
        evidence_question: str,
        rough_results: list[SearchResult],
        retrieval_plan: RetrievalPlan | None,
        final_k: int,
        add_trace: TraceFn,
        max_sync_seconds: float = 8.0,
    ) -> EvidenceWorkflowResult:
        workflow_started = time.monotonic()

        def budget_left() -> float:
            return max(0.0, float(max_sync_seconds) - (time.monotonic() - workflow_started))

        def skip_remaining(step: str) -> None:
            add_trace(
                "decide",
                "evidence_step_skipped",
                {
                    "step": step,
                    "reason": "MCagent has spent the synchronous evidence budget for this turn; answer with current objective evidence and let the Agent decide whether to ask CrawlerAgent for more.",
                    "elapsed_ms": round((time.monotonic() - workflow_started) * 1000),
                    "max_sync_seconds": max_sync_seconds,
                    "selected_count": len(selected),
                    "verdict": report.verdict,
                },
            )

        add_trace("decide", "selecting_evidence", {"candidates": len(rough_results)})
        selected, report = self._selector_factory(final_k).select(evidence_question, rough_results, plan=retrieval_plan)
        selected = self._run_step(
            add_trace,
            "prefer_parent_topic",
            lambda: self._prefer_parent_topic_results(evidence_question, selected, rough_results, final_k),
            fallback=selected,
        )
        if budget_left() <= 0:
            skip_remaining("remaining_evidence_steps")
            add_trace("decide", "evidence_selected", report.to_dict())
            return EvidenceWorkflowResult(selected=selected, report=report)

        modpack_list_selected = self._run_step(
            add_trace,
            "modpack_manifest",
            lambda: self._modpack_manifest_results(evidence_question, rough_results, final_k),
            fallback=[],
        )
        if not modpack_list_selected:
            modpack_list_selected = self._run_step(
                add_trace,
                "local_modpack_manifest",
                lambda: self._supplement_local_modpack_manifest_results(config, evidence_question, final_k),
                fallback=[],
            )
        if modpack_list_selected:
            selected = self._dedupe_results([*modpack_list_selected, *selected], final_k)
            report.verdict = "ok"
            report.reasons = []
            report.selected_count = len(selected)

        if budget_left() <= 0:
            skip_remaining("expensive_supplements")
        elif self._needs_expensive_supplement(selected, report, final_k):
            selected = self._run_step(
                add_trace,
                "project_keyword_supplement",
                lambda: self._supplement_project_keyword_results(config, evidence_question, selected, final_k),
                fallback=selected,
            )
            if budget_left() <= 0:
                skip_remaining("raw_html_supplement")
            else:
                selected = self._run_step(
                    add_trace,
                    "raw_html_supplement",
                    lambda: self._supplement_raw_html_results(config, evidence_question, selected, final_k),
                    fallback=selected,
                )
        else:
            add_trace(
                "decide",
                "evidence_step_skipped",
                {
                    "step": "expensive_supplements",
                    "reason": "EvidenceSelector already found enough objective local evidence; skip slow keyword/raw-HTML supplements for this turn.",
                    "selected_count": len(selected),
                    "final_context_k": final_k,
                    "verdict": report.verdict,
                },
            )
        if budget_left() <= 0:
            skip_remaining("modpack_mod_list_context")
        else:
            selected = self._run_step(
                add_trace,
                "modpack_mod_list_context",
                lambda: self._ensure_modpack_mod_list_context(config, evidence_question, selected, rough_results, final_k),
                fallback=selected,
            )

        if budget_left() <= 0:
            fallback_selected = []
            skip_remaining("theme_fallback")
        else:
            fallback_selected = self._run_step(
                add_trace,
                "theme_fallback",
                lambda: self._fallback_theme_results(evidence_question, rough_results, final_k),
                fallback=[],
            )
        if fallback_selected and len(selected) < min(4, final_k):
            selected = self._dedupe_results([*fallback_selected, *selected], final_k)
            if report.verdict != "ok":
                report.verdict = "ok"
                report.reasons = []

        report.selected_count = len(selected)
        add_trace("decide", "evidence_selected", report.to_dict())
        return EvidenceWorkflowResult(selected=selected, report=report)

    def _needs_expensive_supplement(self, selected: list[SearchResult], report: EvidenceReport, final_k: int) -> bool:
        if report.verdict != "ok":
            return True
        enough_selected = len(selected) >= min(4, max(1, final_k))
        high_confidence = float(report.confidence or 0.0) >= 0.75
        return not (enough_selected and high_confidence)

    def _run_step(
        self,
        add_trace: TraceFn,
        name: str,
        fn: Callable[[], list[SearchResult]],
        *,
        fallback: list[SearchResult] | None = None,
    ) -> list[SearchResult]:
        started = time.monotonic()
        add_trace("decide", "evidence_step_started", {"step": name})
        try:
            results = fn()
        except Exception as exc:
            add_trace(
                "decide",
                "evidence_step_failed",
                {
                    "step": name,
                    "error": f"{type(exc).__name__}: {exc}",
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                },
            )
            if fallback is None:
                raise
            add_trace(
                "decide",
                "evidence_step_recovered",
                {
                    "step": name,
                    "reason": "Optional evidence supplement failed; MCagent will continue with the objective evidence already selected.",
                    "fallback_results": len(fallback),
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                },
            )
            return list(fallback)
        add_trace(
            "decide",
            "evidence_step_done",
            {
                "step": name,
                "results": len(results),
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            },
        )
        return results
