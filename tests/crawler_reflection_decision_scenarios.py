from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.crawler_reflection_decision_service import CrawlerReflectionDecisionService


def assert_equal(name: str, actual, expected) -> None:
    if actual != expected:
        raise AssertionError(f"{name}: expected {expected!r}, got {actual!r}")


def test_execute_pending_contract_clamps_selected_index() -> None:
    decision = CrawlerReflectionDecisionService().normalize(
        {"action": "execute_pending", "selected_index": 9, "reason": "Use the rendered MC百科 page next."},
        pending_count=2,
        planner="test-planner",
    )
    assert_equal("action", decision["action"], "execute_pending")
    assert_equal("selected_index", decision["selected_index"], 0)
    assert_equal("valid", decision["contract"]["valid"], False)
    assert "selected_index_out_of_range" in decision["contract"]["issues"]


def test_replan_without_tasks_requests_llm_materialization() -> None:
    decision = CrawlerReflectionDecisionService().normalize(
        {"action": "replan", "reason": "The current web searches are too generic; plan browser and archive discovery."},
        pending_count=3,
        normalized_tasks=[],
        planner="test-planner",
    )
    assert_equal("action", decision["action"], "replan")
    assert_equal("needs_materialization", decision["contract"]["requires_llm_task_materialization"], True)
    assert "missing_executable_tasks_for_replan" in decision["contract"]["issues"]


def test_add_tasks_with_tasks_ignores_irrelevant_selected_index() -> None:
    decision = CrawlerReflectionDecisionService().normalize(
        {"action": "add_tasks", "selected_index": 99, "reason": "Read the concrete MC百科 page before more broad searches."},
        pending_count=1,
        normalized_tasks=[{"source": "mcmod", "query": "Utopian Journey", "reason": "Inspect objective project page."}],
        planner="test-planner",
    )
    assert_equal("action", decision["action"], "add_tasks")
    assert_equal("selected_index", decision["selected_index"], 0)
    assert_equal("valid", decision["contract"]["valid"], True)
    assert_equal("tasks_count", len(decision["tasks"]), 1)
    assert "selected_index_out_of_range" not in decision["contract"]["issues"]


def test_finish_uses_reason_as_done_summary() -> None:
    decision = CrawlerReflectionDecisionService().normalize(
        {"action": "finish", "reason": "Enough citeable evidence is available for MCagent/RAG."},
        pending_count=0,
        planner="test-planner",
    )
    assert_equal("action", decision["action"], "finish")
    assert_equal("done_summary", decision["done_summary"], "Enough citeable evidence is available for MCagent/RAG.")
    assert_equal("needs_materialization", decision["contract"]["requires_llm_task_materialization"], False)


def test_invalid_action_records_issue_without_inventing_tasks() -> None:
    decision = CrawlerReflectionDecisionService().normalize(
        {"action": "browse_more", "reason": "Need a browser page."},
        pending_count=1,
        normalized_tasks=[{"source": "playwright", "query": "乌托邦探险之旅", "reason": "render project page"}],
        planner="test-planner",
    )
    assert_equal("action", decision["action"], "execute_pending")
    assert_equal("tasks", decision["tasks"], [])
    assert "invalid_or_missing_action" in decision["contract"]["issues"]
    assert "tasks_returned_for_execute_pending" in decision["contract"]["issues"]


if __name__ == "__main__":
    test_execute_pending_contract_clamps_selected_index()
    test_replan_without_tasks_requests_llm_materialization()
    test_add_tasks_with_tasks_ignores_irrelevant_selected_index()
    test_finish_uses_reason_as_done_summary()
    test_invalid_action_records_issue_without_inventing_tasks()
    print("crawler_reflection_decision_scenarios passed")
