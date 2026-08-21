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
    assert_equal("topic_discovery_string_ok", service.should_review(task_source="topic_discovery", result={"returncode": "0"}), True)
    assert_equal("topic_discovery_failed", service.should_review(task_source="topic_discovery", result={"returncode": 1}), False)
    assert_equal("other_source", service.should_review(task_source="mcmod", result={"returncode": 0}), False)


def test_should_review_ignores_malformed_returncodes() -> None:
    service = CrawlerTopicDiscoveryReviewService()
    for name, value in (
        ("bool", False),
        ("non_integral_float", 0.5),
        ("text", "success"),
        ("object", {"code": 0}),
    ):
        assert_equal(f"malformed_{name}", service.should_review(task_source="topic_discovery", result={"returncode": value}), False)
    assert_equal("missing_returncode_keeps_legacy_default", service.should_review(task_source="topic_discovery", result={}), True)


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


def test_record_review_replaces_malformed_expansion_collection() -> None:
    service = CrawlerTopicDiscoveryReviewService()
    for name, malformed in (("text", "unexpected"), ("object", {"at_result_count": 1}), ("tuple", (1, 2))):
        plan = {"discovery_expansions": malformed}
        entry = service.record_review(
            plan=plan,
            result={"query": "topic"},
            task_results_count=4,
            discovered_tasks=[],
        )
        assert_equal(f"{name}_collection", plan["discovery_expansions"], [entry])


if __name__ == "__main__":
    test_should_review_only_successful_topic_discovery()
    test_should_review_ignores_malformed_returncodes()
    test_remaining_slots_never_negative()
    test_record_review_adds_expansion_entry()
    test_record_review_keeps_error_when_no_tasks()
    test_record_review_replaces_malformed_expansion_collection()
    print("crawler_topic_discovery_service_scenarios passed")
