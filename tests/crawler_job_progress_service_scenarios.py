from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.crawler_job_progress_service import CrawlerJobProgressService  # noqa: E402


def assert_equal(name: str, actual, expected) -> None:
    if actual != expected:
        raise AssertionError(f"{name}: expected {expected!r}, got {actual!r}")


def test_planned_payload_contains_plan_and_running_act() -> None:
    payload = CrawlerJobProgressService().planned(topic="落幕曲", task_count=3, plan={"topic": "落幕曲"}, tasks=[{}, {}, {}])
    assert "Crawler planned 落幕曲" in payload["summary"]
    assert_equal("planned_tasks", len(payload["result"]["planned_tasks"]), 3)
    assert_equal("act_status", payload["result"]["loop"][2]["status"], "running")


def test_reflecting_payload_uses_reflection_reason() -> None:
    payload = CrawlerJobProgressService().reflecting(
        reflection={"action": "replan", "reason": "empty sources"},
        task_results=[],
        tasks=[],
        plan={},
    )
    assert "replan" in payload["summary"]
    assert_equal("reflect_note", payload["result"]["loop"][1]["note"], "empty sources")


def test_empty_query_payload_marks_act_blocked() -> None:
    payload = CrawlerJobProgressService().empty_query_blocked(source_label="Jina Reader/Search", task_results=[{}], tasks=[{}], plan={})
    assert "空查询" in payload["summary"]
    assert_equal("act_status", payload["result"]["loop"][2]["status"], "blocked")


def test_executing_payload_mentions_query() -> None:
    payload = CrawlerJobProgressService().executing(
        index=2,
        task_count=5,
        source_label="MC百科搜索",
        query="乌托邦探险之旅",
        reason="project page",
        task_results=[],
        tasks=[],
        plan={},
    )
    assert "乌托邦探险之旅" in payload["summary"]
    assert_equal("act_note", payload["result"]["loop"][2]["note"], "Executing 2/5: 乌托邦探险之旅")


def test_reviewing_and_replanning_payloads_are_running() -> None:
    service = CrawlerJobProgressService()
    review = service.reviewing_candidates(task_results=[], tasks=[], plan={})
    replan = service.replanning(bad_streak=3, replan_count=1, max_replans=2, task_results=[], tasks=[], plan={})
    assert_equal("review_phase", review["result"]["loop"][3]["phase"], "reviewing_candidates")
    assert_equal("replan_status", replan["result"]["loop"][3]["status"], "running")


if __name__ == "__main__":
    test_planned_payload_contains_plan_and_running_act()
    test_reflecting_payload_uses_reflection_reason()
    test_empty_query_payload_marks_act_blocked()
    test_executing_payload_mentions_query()
    test_reviewing_and_replanning_payloads_are_running()
    print("crawler_job_progress_service_scenarios passed")
