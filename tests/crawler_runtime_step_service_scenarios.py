from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.crawler_runtime_step_service import CrawlerRuntimeStepService  # noqa: E402


def assert_equal(name: str, actual, expected) -> None:
    if actual != expected:
        raise AssertionError(f"{name}: expected {expected!r}, got {actual!r}")


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def test_reflection_entry_preserves_contract() -> None:
    reflection = {
        "action": "replan",
        "selected_index": 0,
        "reason": "Need browser-rendered source.",
        "planner": "test",
        "tasks": [{"source": "playwright", "query": "乌托邦探险之旅"}],
        "contract": {"valid": True},
    }
    entry = CrawlerRuntimeStepService().reflection_entry(index=2, reflection=reflection)
    assert_equal("at_index", entry["at_index"], 2)
    assert_equal("contract", entry["contract"], {"valid": True})


def test_replan_inserts_unique_tasks_before_current_index() -> None:
    service = CrawlerRuntimeStepService()
    tasks = [
        {"source": "mcmod", "query": "乌托邦"},
        {"source": "fetch_url", "query": "乌托邦探险之旅"},
    ]
    result = service.apply_action(
        tasks=tasks,
        index=1,
        reflection={"action": "replan", "tasks": [{"source": "playwright", "query": "https://www.mcmod.cn/modpack/1337.html"}]},
        max_total_tasks=4,
    )
    assert_equal("continue_loop", result["continue_loop"], True)
    assert_equal("inserted_source", tasks[1]["source"], "playwright")
    assert_equal("task_count", len(tasks), 3)


def test_duplicate_new_tasks_are_not_inserted() -> None:
    service = CrawlerRuntimeStepService()
    tasks = [{"source": "mcmod", "query": "落幕曲"}]
    result = service.apply_action(
        tasks=tasks,
        index=0,
        reflection={"action": "add_tasks", "tasks": [{"source": "mcmod", "query": "落幕曲"}]},
        max_total_tasks=4,
    )
    assert_equal("continue_loop", result["continue_loop"], False)
    assert_equal("task_count", len(tasks), 1)


def test_selected_index_swaps_next_pending_task() -> None:
    service = CrawlerRuntimeStepService()
    tasks = [
        {"source": "mcmod", "query": "generic"},
        {"source": "playwright", "query": "specific"},
    ]
    result = service.apply_action(
        tasks=tasks,
        index=0,
        reflection={"action": "execute_pending", "selected_index": 1, "reason": "Browser result is better."},
        max_total_tasks=4,
    )
    assert_equal("selected_offset", result["selected_offset"], 1)
    assert_equal("first_source", tasks[0]["source"], "playwright")


def test_finish_returns_finish_reason() -> None:
    service = CrawlerRuntimeStepService()
    result = service.apply_action(
        tasks=[],
        index=0,
        reflection={"action": "finish", "done_summary": "Enough data collected."},
        max_total_tasks=4,
    )
    assert_equal("finished", result["finished"], True)
    assert_equal("finish_reason", result["finish_reason"], "Enough data collected.")


def test_llm_plan_first_task_does_not_need_extra_reflection() -> None:
    service = CrawlerRuntimeStepService()
    assert_equal(
        "llm_plan_first",
        service.should_reflect_before_task(plan={"strategy": "crawler_llm_planner"}, task_results=[], index=0),
        False,
    )
    assert_equal(
        "quick_recovery_first",
        service.should_reflect_before_task(plan={"strategy": "quick_recovery_llm_plan_after_planner_error"}, task_results=[], index=0),
        False,
    )


def test_rule_fallback_still_requires_crawler_reflection_before_tools() -> None:
    service = CrawlerRuntimeStepService()
    assert_equal(
        "rule_fallback_first",
        service.should_reflect_before_task(plan={"strategy": "target_fallback_after_llm_planner_error"}, task_results=[], index=0),
        True,
    )
    assert_equal(
        "after_result",
        service.should_reflect_before_task(plan={"strategy": "crawler_llm_planner"}, task_results=[{"source": "web_discovery"}], index=1),
        True,
    )


def test_initial_llm_plan_entry_is_trace_only() -> None:
    service = CrawlerRuntimeStepService()
    task = {"source": "web_discovery", "query": "Utopian Journey"}
    entry = service.initial_llm_plan_entry(task=task)
    assert_equal("action", entry["action"], "execute_pending")
    assert_equal("planner", entry["planner"], "crawler_llm_planner")
    assert_true("contains_task", entry["tasks"] == [task])
    assert_true("valid_contract", entry["contract"]["valid"])


if __name__ == "__main__":
    test_reflection_entry_preserves_contract()
    test_replan_inserts_unique_tasks_before_current_index()
    test_duplicate_new_tasks_are_not_inserted()
    test_selected_index_swaps_next_pending_task()
    test_finish_returns_finish_reason()
    test_llm_plan_first_task_does_not_need_extra_reflection()
    test_rule_fallback_still_requires_crawler_reflection_before_tools()
    test_initial_llm_plan_entry_is_trace_only()
    print("crawler_runtime_step_service_scenarios passed")

