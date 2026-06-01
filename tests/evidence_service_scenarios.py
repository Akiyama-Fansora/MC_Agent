from __future__ import annotations

from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.evidence_selector import EvidenceReport, EvidenceSelector  # noqa: E402
from mcagent.evidence_service import EvidenceWorkflowService  # noqa: E402
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


class FakeSelector:
    def __init__(self, final_context_k: int, selected: list[SearchResult], report: EvidenceReport, calls: list[dict[str, Any]]) -> None:
        self.final_context_k = final_context_k
        self._selected = selected
        self._report = report
        self._calls = calls

    def select(self, question: str, candidates: list[SearchResult], plan: RetrievalPlan | None = None):
        self._calls.append({"question": question, "candidates": len(candidates), "plan": plan, "final_context_k": self.final_context_k})
        return list(self._selected), self._report


def assert_equal(name: str, actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"{name}: expected {expected!r}, got {actual!r}")


def assert_true(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name}: {detail}")


def base_report(*, verdict: str = "insufficient") -> EvidenceReport:
    return EvidenceReport(
        verdict=verdict,
        topic_detected="project",
        confidence=0.5,
        selected_count=1,
        candidate_count=3,
        reasons=["not enough"],
        final_context_k=4,
    )


def build_service(
    *,
    selector_selected: list[SearchResult],
    selector_report: EvidenceReport,
    selector_calls: list[dict[str, Any]],
    modpack_results: list[SearchResult] | None = None,
    fallback_results: list[SearchResult] | None = None,
    call_log: list[str] | None = None,
) -> EvidenceWorkflowService:
    calls = call_log if call_log is not None else []

    return EvidenceWorkflowService(
        selector_factory=lambda final_k: FakeSelector(final_k, selector_selected, selector_report, selector_calls),
        prefer_parent_topic_results=lambda question, selected, rough, final_k: calls.append("prefer_parent") or selected,
        modpack_manifest_results=lambda question, rough, final_k: calls.append("modpack_manifest") or list(modpack_results or []),
        supplement_local_modpack_manifest_results=lambda config, question, final_k: calls.append("local_modpack_manifest") or [],
        supplement_project_keyword_results=lambda config, question, selected, final_k: calls.append("project_keywords") or selected,
        supplement_raw_html_results=lambda config, question, selected, final_k: calls.append("raw_html") or selected,
        ensure_modpack_mod_list_context=lambda config, question, selected, rough, final_k: calls.append("modpack_mod_list") or selected,
        fallback_theme_results=lambda question, rough, final_k: calls.append("fallback_theme") or list(fallback_results or []),
        dedupe_results=lambda results, limit: list(results)[:limit],
    )


def test_evidence_service_runs_selector_and_supplements_in_order() -> None:
    selector_calls: list[dict[str, Any]] = []
    call_log: list[str] = []
    traces: list[dict[str, Any]] = []
    plan = RetrievalPlan(topic="utopia")
    service = build_service(
        selector_selected=[make_result(1)],
        selector_report=base_report(verdict="ok"),
        selector_calls=selector_calls,
        call_log=call_log,
    )

    result = service.select(
        object(),
        evidence_question="utopia gameplay",
        rough_results=[make_result(1), make_result(2), make_result(3)],
        retrieval_plan=plan,
        final_k=4,
        add_trace=lambda stage, status, detail=None: traces.append({"stage": stage, "status": status, "detail": detail}) or traces[-1],
    )

    assert_equal("selector_question", selector_calls[0]["question"], "utopia gameplay")
    assert_equal("selector_final_k", selector_calls[0]["final_context_k"], 4)
    assert_equal("selected_count", len(result.selected), 1)
    assert_equal(
        "call_order",
        call_log,
        ["prefer_parent", "modpack_manifest", "local_modpack_manifest", "project_keywords", "raw_html", "modpack_mod_list", "fallback_theme"],
    )
    assert_equal("trace_statuses", [item["status"] for item in traces], ["selecting_evidence", "evidence_selected"])


def test_modpack_manifest_evidence_can_upgrade_objective_report() -> None:
    selector_calls: list[dict[str, Any]] = []
    traces: list[dict[str, Any]] = []
    service = build_service(
        selector_selected=[make_result(1)],
        selector_report=base_report(verdict="insufficient"),
        selector_calls=selector_calls,
        modpack_results=[make_result(9, "Modpack manifest")],
    )

    result = service.select(
        object(),
        evidence_question="modpack list",
        rough_results=[make_result(1)],
        retrieval_plan=None,
        final_k=4,
        add_trace=lambda stage, status, detail=None: traces.append({"stage": stage, "status": status, "detail": detail}) or traces[-1],
    )

    assert_equal("verdict", result.report.verdict, "ok")
    assert_equal("reasons", result.report.reasons, [])
    assert_equal("selected_first", result.selected[0].title, "Modpack manifest 9")
    assert_equal("report_selected_count", result.report.selected_count, len(result.selected))


def test_fallback_theme_evidence_can_recover_sparse_selection() -> None:
    selector_calls: list[dict[str, Any]] = []
    service = build_service(
        selector_selected=[],
        selector_report=base_report(verdict="insufficient"),
        selector_calls=selector_calls,
        fallback_results=[make_result(5, "Theme")],
    )

    result = service.select(
        object(),
        evidence_question="theme",
        rough_results=[make_result(1)],
        retrieval_plan=None,
        final_k=4,
        add_trace=lambda stage, status, detail=None: {"stage": stage, "status": status, "detail": detail},
    )

    assert_equal("verdict", result.report.verdict, "ok")
    assert_equal("selected_count", len(result.selected), 1)
    assert_true("fallback_selected", result.selected[0].title.startswith("Theme"))


def test_create_accepted_project_pages_are_strong_sources() -> None:
    selector = EvidenceSelector(final_context_k=4)
    accepted_mcmod = SearchResult(
        rank=1,
        score=9.5,
        chunk_id=1,
        document_id=1,
        chunk_index=0,
        title="\u673a\u68b0\u52a8\u529b (Create) - MC\u767e\u79d1",
        source_path=r"D:\magic\MC_Agent\data\crawler_exports\mcmod\run\accepted_by_crawler\create.html",
        url="https://www.mcmod.cn/class/2021.html",
        text="Create / \u673a\u68b0\u52a8\u529b rotational power stress automation.",
        metadata={},
    )
    accepted_modrinth = SearchResult(
        rank=2,
        score=8.5,
        chunk_id=2,
        document_id=2,
        chunk_index=0,
        title="Create",
        source_path=r"D:\magic\MC_Agent\data\crawler_exports\modrinth_agent\run\accepted_by_crawler\mod_create.md",
        url="https://modrinth.com/mod/create",
        text="Create mod supported loaders versions and Ponder documentation.",
        metadata={},
    )

    selected, report = selector.select(
        "Explain Create mod rotational power and stress from local evidence.",
        [accepted_mcmod, accepted_modrinth],
    )

    assert_equal("verdict", report.verdict, "ok")
    assert_true("selected", len(selected) >= 2)
    assert_equal("reasons", report.reasons, [])


if __name__ == "__main__":
    test_evidence_service_runs_selector_and_supplements_in_order()
    test_modpack_manifest_evidence_can_upgrade_objective_report()
    test_fallback_theme_evidence_can_recover_sparse_selection()
    test_create_accepted_project_pages_are_strong_sources()
    print("evidence_service_scenarios: ok")
