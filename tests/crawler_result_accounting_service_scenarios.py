from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.crawler_result_accounting_service import CrawlerResultAccountingService  # noqa: E402


def assert_equal(name: str, actual, expected) -> None:
    if actual != expected:
        raise AssertionError(f"{name}: expected {expected!r}, got {actual!r}")


def apply(result: dict, source: str = "mcmod", delivery: str = "MCagent/RAG") -> dict:
    return CrawlerResultAccountingService().apply(
        result=result,
        task_source=source,
        delivery_target=delivery,
        followup_query="乌托邦探险之旅",
    )


def test_matched_records_are_success_and_need_ingest() -> None:
    result = {"returncode": 0, "manifest_stats": {"records": 3}, "topic_validation": {"matched": True}}
    accounting = apply(result)
    assert_equal("success", accounting["success_delta"], 1)
    assert_equal("needs_ingest", accounting["needs_ingest"], True)
    assert_equal("ingest_deferred", result["ingest_deferred"], "Crawler will ingest once after the collection loop finishes.")


def test_browser_collect_for_human_skips_ingest() -> None:
    result = {"returncode": 0, "manifest_stats": {"records": 50}}
    accounting = apply(result, source="browser_collect", delivery="human")
    assert_equal("success", accounting["success_delta"], 1)
    assert_equal("needs_ingest", accounting["needs_ingest"], False)
    assert_equal("ingest_skipped", result["ingest_skipped"], "Structured browser output was saved to the requested directory for the human user.")


def test_modpack_download_creates_internal_followup() -> None:
    result = {"returncode": 0, "manifest_stats": {"records": 1, "downloads": 1}}
    accounting = apply(result, source="modpack_download")
    assert_equal("success", accounting["success_delta"], 1)
    assert_equal("archive_downloaded", result["archive_downloaded"], True)
    assert_equal("followup_source", accounting["followup_task"]["source"], "modpack_internal")


def test_off_topic_records_are_failure() -> None:
    result = {"returncode": 0, "manifest_stats": {"records": 2}, "topic_validation": {"matched": False, "reason": "off_topic"}}
    accounting = apply(result)
    assert_equal("failure", accounting["failure_delta"], 1)
    assert_equal("off_topic", result["off_topic_result"], True)


def test_topic_discovery_counts_candidate_only() -> None:
    result = {"returncode": 0, "manifest_stats": {"records": 8}}
    accounting = apply(result, source="topic_discovery")
    assert_equal("candidate", accounting["candidate_delta"], 1)
    assert_equal("candidate_only", result["candidate_only"], True)


if __name__ == "__main__":
    test_matched_records_are_success_and_need_ingest()
    test_browser_collect_for_human_skips_ingest()
    test_modpack_download_creates_internal_followup()
    test_off_topic_records_are_failure()
    test_topic_discovery_counts_candidate_only()
    print("crawler_result_accounting_service_scenarios passed")
