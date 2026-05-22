from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.crawler_reflection_service import CrawlerReflectionSnapshotService  # noqa: E402


def assert_equal(name: str, actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"{name}: expected {expected!r}, got {actual!r}")


def assert_true(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name}: {detail}")


def test_snapshot_counts_observations_and_pressure() -> None:
    snapshot = CrawlerReflectionSnapshotService().build(
        plan={
            "topic": "乌托邦探险之旅",
            "delivery_target": "MCagent/RAG",
            "coverage_goals": ["完整模组列表"],
            "success_criteria": ["可引用"],
            "sources": ["mcmod", "firecrawl"],
        },
        task_results=[
            {"source": "mcmod", "query": "乌托邦", "returncode": 0, "manifest_stats": {"records": 0}, "empty_result": True},
            {"source": "firecrawl", "query": "乌托邦 玩法", "returncode": 1, "output": "HTTP 429 quota exceeded"},
        ],
        pending_tasks=[{"source": "playwright", "query": "乌托邦探险之旅", "reason": "render project page"}],
    )

    assert_equal("topic", snapshot["plan"]["topic"], "乌托邦探险之旅")
    assert_equal("recent_count", len(snapshot["recent_results"]), 2)
    assert_equal("pending_count", len(snapshot["pending_tasks"]), 1)
    assert_equal("empty_count", snapshot["observation_statuses"].get("empty"), 1)
    assert_equal("quota_count", snapshot["observation_statuses"].get("quota_limited"), 1)
    assert_equal("pressure", snapshot["pressure"], "quota_limited_change_source")
    assert_true("retryable_count", int(snapshot["retryable_recent_results"]) >= 1)


def test_snapshot_detects_poor_yield_replan_pressure() -> None:
    snapshot = CrawlerReflectionSnapshotService().build(
        plan={"topic": "落幕曲"},
        task_results=[
            {"source": "jina", "returncode": 0, "empty_result": True, "manifest_stats": {"records": 0}},
            {"source": "web_discovery", "returncode": 0, "off_topic_result": True, "manifest_stats": {"records": 1}},
            {"source": "tavily", "returncode": 0, "empty_result": True, "manifest_stats": {"records": 0}},
        ],
        pending_tasks=[{"source": "mcmod", "query": "落幕曲 Boss"}],
    )

    assert_equal("pressure", snapshot["pressure"], "poor_yield_replan_queries_or_sources")
    assert_equal("pending_source", snapshot["pending_tasks"][0]["source"], "mcmod")


if __name__ == "__main__":
    test_snapshot_counts_observations_and_pressure()
    test_snapshot_detects_poor_yield_replan_pressure()
    print("crawler_reflection_service_scenarios: ok")
