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


def test_good_result_resets_streak() -> None:
    result = {"returncode": 0, "manifest_stats": {"records": 2}, "topic_validation": {"matched": True}}
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


if __name__ == "__main__":
    test_bad_result_increments_streak()
    test_good_result_resets_streak()
    test_records_pending_review_keeps_pressure()
    test_should_replan_requires_planner_source_and_no_success()
    test_should_finish_after_useful_low_yield()
    test_should_finish_after_no_success_low_yield()
    print("crawler_loop_control_service_scenarios passed")
