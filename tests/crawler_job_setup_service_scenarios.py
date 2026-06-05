from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.crawler_job_setup_service import CrawlerJobSetupService  # noqa: E402


def assert_equal(name: str, actual, expected) -> None:
    if actual != expected:
        raise AssertionError(f"{name}: expected {expected!r}, got {actual!r}")


def test_planner_source_aliases_are_objective() -> None:
    service = CrawlerJobSetupService()
    assert_equal("planner", service.is_planner_source("planner"), True)
    assert_equal("smart", service.is_planner_source("smart"), True)
    assert_equal("orchestrator", service.is_planner_source("orchestrator"), True)
    assert_equal("mcmod", service.is_planner_source("mcmod"), False)


def test_single_source_task_prefers_payload_query_then_question() -> None:
    service = CrawlerJobSetupService()
    explicit = service.single_source_tasks(source="mcmod", payload={"query": "乌托邦"}, question="fallback")
    fallback = service.single_source_tasks(source="mcmod", payload={}, question="落幕曲")
    assert_equal("explicit_query", explicit[0]["query"], "乌托邦")
    assert_equal("fallback_question", fallback[0]["query"], "落幕曲")
    assert_equal("source", explicit[0]["source"], "mcmod")


def test_fallback_plan_preserves_tasks_without_planning() -> None:
    tasks = [{"source": "fetch_url", "query": "乌托邦探险之旅"}]
    plan = CrawlerJobSetupService().fallback_plan(tasks=tasks)
    assert_equal("strategy", plan["strategy"], "fallback_all_source_tasks")
    assert_equal("same_tasks", plan["tasks"], tasks)


def test_limits_respect_payload_and_total_cap() -> None:
    service = CrawlerJobSetupService()
    defaults = service.limits(payload={}, tasks=[{}])
    capped = service.limits(payload={"max_replans": 4, "max_tasks": 40}, tasks=[{} for _ in range(3)])
    explicit = service.limits(payload={"max_tasks": 8}, tasks=[{} for _ in range(8)])
    assert_equal("default_replans", defaults["max_replans"], 2)
    assert_equal("default_total", defaults["max_total_tasks"], 7)
    assert_equal("custom_replans", capped["max_replans"], 4)
    assert_equal("total_cap", capped["max_total_tasks"], 32)
    assert_equal("explicit_budget_total", explicit["max_total_tasks"], 8)


def test_stopped_updates_do_not_own_end_time() -> None:
    service = CrawlerJobSetupService()
    before = service.stopped_update(stage="before_plan")
    planning = service.stopped_update(stage="planning", plan={"stopped": True})
    after = service.stopped_update(stage="after_plan", plan={"topic": "乌托邦"}, tasks=[{"query": "乌托邦"}])
    assert_equal("before_status", before["status"], "stopped")
    assert_equal("no_ended_at", "ended_at" in before, False)
    assert_equal("planning_tasks", planning["result"]["planned_tasks"], [])
    assert_equal("after_task", after["result"]["planned_tasks"][0]["query"], "乌托邦")


if __name__ == "__main__":
    test_planner_source_aliases_are_objective()
    test_single_source_task_prefers_payload_query_then_question()
    test_fallback_plan_preserves_tasks_without_planning()
    test_limits_respect_payload_and_total_cap()
    test_stopped_updates_do_not_own_end_time()
    print("crawler_job_setup_service_scenarios passed")

