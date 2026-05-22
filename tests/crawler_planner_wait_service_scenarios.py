from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.crawler_planner_wait_service import CrawlerPlannerWaitService  # noqa: E402


def assert_equal(name: str, actual, expected) -> None:
    if actual != expected:
        raise AssertionError(f"{name}: expected {expected!r}, got {actual!r}")


def test_context_prefers_authoritative_goal_and_keeps_handoff() -> None:
    service = CrawlerPlannerWaitService()
    context = service.context(
        question="让 Crawler 补资料",
        session_summary={
            "handoff_brief": "完整交接说明",
            "delivery_target": "MCagent/RAG",
            "collection_target": "乌托邦探险之旅玩法缺口",
        },
    )
    assert_equal("topic", context["planner_topic"], "乌托邦探险之旅玩法缺口")
    assert_equal("handoff", context["handoff_brief"], "完整交接说明")
    assert_equal("delivery", context["delivery_target"], "MCagent/RAG")


def test_context_falls_back_to_question_without_summary() -> None:
    context = CrawlerPlannerWaitService().context(question="落幕曲 BOSS 列表", session_summary=None)
    assert_equal("topic", context["planner_topic"], "落幕曲 BOSS 列表")
    assert_equal("handoff", context["handoff_brief"], "")
    assert_equal("delivery", context["delivery_target"], "")


def test_stopped_plan_is_planner_contract() -> None:
    plan = CrawlerPlannerWaitService().stopped_plan(planner_topic="乌托邦", handoff_brief="brief")
    assert_equal("stopped", plan["stopped"], True)
    assert_equal("strategy", plan["strategy"], "stopped_before_planner_finished")
    assert_equal("tasks", plan["tasks"], [])


def test_waiting_update_keeps_loop_observable() -> None:
    update = CrawlerPlannerWaitService().waiting_update(
        elapsed_seconds=15,
        planner_topic="乌托邦探险之旅" * 10,
        handoff_brief="brief",
        delivery_target="human",
    )
    assert "已思考 15 秒" in update["summary"]
    assert_equal("source", update["result"]["source"], "planner")
    assert_equal("delivery", update["result"]["plan"]["delivery_target"], "human")
    assert_equal("understand_status", update["result"]["loop"][0]["status"], "running")
    assert_equal("planned_tasks", update["result"]["planned_tasks"], [])


if __name__ == "__main__":
    test_context_prefers_authoritative_goal_and_keeps_handoff()
    test_context_falls_back_to_question_without_summary()
    test_stopped_plan_is_planner_contract()
    test_waiting_update_keeps_loop_observable()
    print("crawler_planner_wait_service_scenarios passed")
