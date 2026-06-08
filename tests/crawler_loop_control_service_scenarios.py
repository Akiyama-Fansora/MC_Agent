from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.crawler_loop_control_service import CrawlerLoopControlService  # noqa: E402


def assert_equal(name: str, actual, expected) -> None:
    if actual != expected:
        raise AssertionError(f"{name}: expected {expected!r}, got {actual!r}")


def test_bad_result_increments_streak() -> None:
    result = {"returncode": 0, "manifest_stats": {"records": 0}, "empty_result": True}
    signal = CrawlerLoopControlService().update_bad_streak(result=result, current_bad_streak=2)
    assert_equal("bad", signal["bad"], True)
    assert_equal("bad_streak", signal["bad_streak"], 3)
    assert_equal("observation_status", result["observation"]["status"], "empty")


def test_matched_result_still_waits_for_review() -> None:
    result = {"returncode": 0, "manifest_stats": {"records": 2}, "topic_validation": {"matched": True}}
    signal = CrawlerLoopControlService().update_bad_streak(result=result, current_bad_streak=2)
    assert_equal("bad", signal["bad"], True)
    assert_equal("bad_streak", signal["bad_streak"], 3)
    assert_equal("observation_status", result["observation"]["status"], "records_pending_review")


def test_explicitly_accepted_result_resets_streak() -> None:
    result = {"returncode": 0, "manifest_stats": {"records": 2}, "topic_validation": {"matched": True, "crawler_review_action": "accept"}}
    signal = CrawlerLoopControlService().update_bad_streak(result=result, current_bad_streak=2)
    assert_equal("bad", signal["bad"], False)
    assert_equal("bad_streak", signal["bad_streak"], 0)


def test_records_pending_review_keeps_pressure() -> None:
    result = {"returncode": 0, "manifest_stats": {"records": 2}}
    signal = CrawlerLoopControlService().update_bad_streak(result=result, current_bad_streak=2)
    assert_equal("bad", signal["bad"], True)
    assert_equal("bad_streak", signal["bad_streak"], 3)
    assert_equal("observation_status", result["observation"]["status"], "records_pending_review")


def test_should_replan_requires_planner_source_and_no_success() -> None:
    service = CrawlerLoopControlService()
    assert_equal(
        "planner_replans",
        service.should_replan(
            source="planner",
            success_count=0,
            bad_streak=3,
            replan_count=0,
            max_replans=2,
            task_count=5,
            max_total_tasks=10,
        ),
        True,
    )
    assert_equal(
        "human_source_no_replan",
        service.should_replan(
            source="mcmod",
            success_count=0,
            bad_streak=3,
            replan_count=0,
            max_replans=2,
            task_count=5,
            max_total_tasks=10,
        ),
        False,
    )
    assert_equal(
        "success_no_replan",
        service.should_replan(
            source="planner",
            success_count=1,
            bad_streak=3,
            replan_count=0,
            max_replans=2,
            task_count=5,
            max_total_tasks=10,
        ),
        False,
    )


def test_should_replan_after_single_failed_plan_is_exhausted() -> None:
    service = CrawlerLoopControlService()
    assert_equal(
        "replan_exhausted_single_task",
        service.should_replan_after_plan_exhausted(
            source="planner",
            success_count=0,
            bad_streak=1,
            replan_count=0,
            max_replans=2,
            current_index=1,
            task_count=1,
            max_total_tasks=8,
        ),
        True,
    )
    assert_equal(
        "keep_when_pending_tasks_exist",
        service.should_replan_after_plan_exhausted(
            source="planner",
            success_count=0,
            bad_streak=1,
            replan_count=0,
            max_replans=2,
            current_index=1,
            task_count=3,
            max_total_tasks=8,
        ),
        False,
    )


def test_should_finish_after_useful_low_yield() -> None:
    service = CrawlerLoopControlService()
    assert_equal(
        "finish_after_success_and_bad_streak",
        service.should_finish_after_useful_low_yield(
            source="planner",
            success_count=1,
            bad_streak=4,
            executed_count=8,
        ),
        True,
    )
    assert_equal(
        "no_finish_without_success",
        service.should_finish_after_useful_low_yield(
            source="planner",
            success_count=0,
            bad_streak=4,
            executed_count=8,
        ),
        False,
    )
    assert_equal(
        "no_finish_for_single_source",
        service.should_finish_after_useful_low_yield(
            source="mcmod",
            success_count=1,
            bad_streak=4,
            executed_count=8,
        ),
        False,
    )


def test_should_finish_after_no_success_low_yield() -> None:
    service = CrawlerLoopControlService()
    assert_equal(
        "finish_after_repeated_empty_results",
        service.should_finish_after_no_success_low_yield(
            source="planner",
            success_count=0,
            bad_streak=3,
            executed_count=6,
        ),
        True,
    )
    assert_equal(
        "keep_trying_before_minimum",
        service.should_finish_after_no_success_low_yield(
            source="planner",
            success_count=0,
            bad_streak=3,
            executed_count=5,
        ),
        False,
    )


def test_should_finish_after_enough_success_at_task_budget() -> None:
    service = CrawlerLoopControlService()
    assert_equal(
        "finish_at_budget",
        service.should_finish_after_enough_success(
            source="planner",
            success_count=3,
            executed_count=8,
            task_count=20,
            max_total_tasks=20,
        ),
        True,
    )
    assert_equal(
        "keep_below_budget",
        service.should_finish_after_enough_success(
            source="planner",
            success_count=3,
            executed_count=8,
            task_count=19,
            max_total_tasks=20,
        ),
        False,
    )
    assert_equal(
        "keep_before_enough_evidence",
        service.should_finish_after_enough_success(
            source="planner",
            success_count=2,
            executed_count=8,
            task_count=20,
            max_total_tasks=20,
        ),
        False,
    )


def test_should_finish_after_gap_probe_satisfied() -> None:
    service = CrawlerLoopControlService()
    assert_equal(
        "finish_gap_probe",
        service.should_finish_after_gap_probe_satisfied(
            source="planner",
            task_results=[
                {
                    "source": "mcagent_context",
                    "returncode": 0,
                    "mcagent_gap_summary": "- No explicit gap list was found; use coverage goals.",
                    "manifest_stats": {"records": 1},
                },
                {
                    "source": "modpack_download",
                    "returncode": 0,
                    "manifest_stats": {"records": 2, "candidates": 1, "downloads": 0},
                },
            ],
            candidate_count=1,
            success_count=0,
        ),
        True,
    )
    assert_equal(
        "keep_when_explicit_gaps",
        service.should_finish_after_gap_probe_satisfied(
            source="planner",
            task_results=[
                {"source": "mcagent_context", "returncode": 0, "mcagent_gap_summary": "- 缺少完整模组列表", "manifest_stats": {"records": 1}},
                {"source": "web_discovery", "returncode": 0, "manifest_stats": {"records": 1}},
            ],
            candidate_count=1,
            success_count=0,
        ),
        False,
    )


def test_should_finish_after_context_plus_external_checkpoint() -> None:
    service = CrawlerLoopControlService()
    task_results = [
        {"source": "mcagent_context", "returncode": 0, "manifest_stats": {"records": 1}},
        {"source": "web_discovery", "returncode": 0, "manifest_stats": {"records": 1, "candidates": 0}},
        {"source": "playwright", "returncode": 0, "manifest_stats": {"records": 0}},
        {"source": "playwright", "returncode": 0, "manifest_stats": {"records": 0}},
    ]
    assert_equal(
        "finish_checkpoint",
        service.should_finish_after_context_plus_external_checkpoint(
            source="planner",
            task_results=task_results,
            candidate_count=1,
            success_count=1,
            bad_streak=2,
            executed_count=4,
        ),
        True,
    )
    assert_equal(
        "keep_without_context",
        service.should_finish_after_context_plus_external_checkpoint(
            source="planner",
            task_results=task_results[1:],
            candidate_count=1,
            success_count=1,
            bad_streak=2,
            executed_count=4,
        ),
        False,
    )
    assert_equal(
        "keep_before_low_yield",
        service.should_finish_after_context_plus_external_checkpoint(
            source="planner",
            task_results=task_results,
            candidate_count=1,
            success_count=1,
            bad_streak=1,
            executed_count=4,
        ),
        False,
    )


def test_should_finish_after_gap_summary_handoff_success() -> None:
    service = CrawlerLoopControlService()
    assert_equal(
        "finish_after_gap_handoff_success",
        service.should_finish_after_gap_summary_handoff_success(
            source="planner",
            plan={
                "mcagent_gap_summary": "本地资料库目前有入口页，但缺少玩法路线。",
                "delivery_target": "MCagent/RAG",
            },
            task_results=[],
            success_count=1,
            executed_count=1,
        ),
        True,
    )
    assert_equal(
        "keep_without_success",
        service.should_finish_after_gap_summary_handoff_success(
            source="planner",
            plan={"mcagent_gap_summary": "缺少资料", "delivery_target": "MCagent/RAG"},
            task_results=[],
            success_count=0,
            executed_count=1,
        ),
        False,
    )
    assert_equal(
        "keep_without_gap_handoff",
        service.should_finish_after_gap_summary_handoff_success(
            source="planner",
            plan={"delivery_target": "human"},
            task_results=[],
            success_count=1,
            executed_count=1,
        ),
        False,
    )
    assert_equal(
        "finish_when_gap_summary_is_in_context_result",
        service.should_finish_after_gap_summary_handoff_success(
            source="planner",
            plan={"delivery_target": "MCagent/RAG"},
            task_results=[
                {"source": "mcagent_context", "returncode": 0, "mcagent_gap_summary": "本地资料库目前有入口页，但缺少玩法路线。"},
                {"source": "playwright", "returncode": 0, "manifest_stats": {"records": 1}},
            ],
            success_count=1,
            executed_count=2,
        ),
        True,
    )


def test_should_finish_after_rag_success_checkpoint() -> None:
    service = CrawlerLoopControlService()
    assert_equal(
        "finish_fallback_rag_after_success",
        service.should_finish_after_rag_success_checkpoint(
            source="planner",
            plan={"delivery_target": "MCagent/RAG", "strategy": "target_fallback_after_llm_planner_error"},
            success_count=1,
            executed_count=3,
        ),
        True,
    )
    assert_equal(
        "keep_without_success",
        service.should_finish_after_rag_success_checkpoint(
            source="planner",
            plan={"delivery_target": "MCagent/RAG", "strategy": "target_fallback_after_llm_planner_error"},
            success_count=0,
            executed_count=3,
        ),
        False,
    )
    assert_equal(
        "keep_human_delivery",
        service.should_finish_after_rag_success_checkpoint(
            source="planner",
            plan={"delivery_target": "human", "strategy": "target_fallback_after_llm_planner_error"},
            success_count=1,
            executed_count=3,
        ),
        False,
    )


if __name__ == "__main__":
    test_bad_result_increments_streak()
    test_matched_result_still_waits_for_review()
    test_explicitly_accepted_result_resets_streak()
    test_records_pending_review_keeps_pressure()
    test_should_replan_requires_planner_source_and_no_success()
    test_should_replan_after_single_failed_plan_is_exhausted()
    test_should_finish_after_useful_low_yield()
    test_should_finish_after_no_success_low_yield()
    test_should_finish_after_enough_success_at_task_budget()
    test_should_finish_after_gap_probe_satisfied()
    test_should_finish_after_context_plus_external_checkpoint()
    test_should_finish_after_gap_summary_handoff_success()
    test_should_finish_after_rag_success_checkpoint()
    print("crawler_loop_control_service_scenarios passed")
