from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.crawler_topic_discovery_service import CrawlerTopicDiscoveryReviewService  # noqa: E402


def assert_equal(name: str, actual, expected) -> None:
    if actual != expected:
        raise AssertionError(f"{name}: expected {expected!r}, got {actual!r}")


def test_should_review_only_successful_topic_discovery() -> None:
    service = CrawlerTopicDiscoveryReviewService()
    assert_equal("topic_discovery_ok", service.should_review(task_source="topic_discovery", result={"returncode": 0}), True)
    assert_equal("topic_discovery_failed", service.should_review(task_source="topic_discovery", result={"returncode": 1}), False)
    assert_equal("other_source", service.should_review(task_source="mcmod", result={"returncode": 0}), False)


def test_remaining_slots_never_negative() -> None:
    service = CrawlerTopicDiscoveryReviewService()
    assert_equal("positive", service.remaining_slots(max_total_tasks=10, current_task_count=4), 6)
    assert_equal("zero", service.remaining_slots(max_total_tasks=10, current_task_count=12), 0)


def test_record_review_adds_expansion_entry() -> None:
    service = CrawlerTopicDiscoveryReviewService()
    plan: dict = {}
    entry = service.record_review(
        plan=plan,
        result={"query": "乌托邦"},
        task_results_count=3,
        discovered_tasks=[{"source": "mcmod", "query": "乌托邦探险之旅"}],
    )
    assert_equal("entry_count", len(plan["discovery_expansions"]), 1)
    assert_equal("source_query", entry["source_query"], "乌托邦")
    assert_equal("new_task_source", entry["new_tasks"][0]["source"], "mcmod")


def test_record_review_keeps_error_when_no_tasks() -> None:
    service = CrawlerTopicDiscoveryReviewService()
    plan: dict = {}
    entry = service.record_review(
        plan=plan,
        result={"query": "乌托邦", "topic_discovery_review_error": "LLM timeout"},
        task_results_count=2,
        discovered_tasks=[],
    )
    assert_equal("error", entry["error"], "LLM timeout")
    assert_equal("new_tasks", entry["new_tasks"], [])


if __name__ == "__main__":
    test_should_review_only_successful_topic_discovery()
    test_remaining_slots_never_negative()
    test_record_review_adds_expansion_entry()
    test_record_review_keeps_error_when_no_tasks()
    print("crawler_topic_discovery_service_scenarios passed")
