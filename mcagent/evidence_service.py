from __future__ import annotations

from dataclasses import dataclass
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
    ) -> EvidenceWorkflowResult:
        add_trace("decide", "selecting_evidence", {"candidates": len(rough_results)})
        selected, report = self._selector_factory(final_k).select(evidence_question, rough_results, plan=retrieval_plan)
        selected = self._prefer_parent_topic_results(evidence_question, selected, rough_results, final_k)

        modpack_list_selected = self._modpack_manifest_results(evidence_question, rough_results, final_k)
        if not modpack_list_selected:
            modpack_list_selected = self._supplement_local_modpack_manifest_results(config, evidence_question, final_k)
        if modpack_list_selected:
            selected = self._dedupe_results([*modpack_list_selected, *selected], final_k)
            report.verdict = "ok"
            report.reasons = []
            report.selected_count = len(selected)

        selected = self._supplement_project_keyword_results(config, evidence_question, selected, final_k)
        selected = self._supplement_raw_html_results(config, evidence_question, selected, final_k)
        selected = self._ensure_modpack_mod_list_context(config, evidence_question, selected, rough_results, final_k)

        fallback_selected = self._fallback_theme_results(evidence_question, rough_results, final_k)
        if fallback_selected and len(selected) < min(4, final_k):
            selected = self._dedupe_results([*fallback_selected, *selected], final_k)
            if report.verdict != "ok":
                report.verdict = "ok"
                report.reasons = []
                report.selected_count = len(selected)

        add_trace("decide", "evidence_selected", report.to_dict())
        return EvidenceWorkflowResult(selected=selected, report=report)
