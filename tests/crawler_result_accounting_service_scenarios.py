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


def test_matched_records_wait_for_crawler_review() -> None:
    result = {"returncode": 0, "manifest_stats": {"records": 3}, "topic_validation": {"matched": True}}
    accounting = apply(result)
    assert_equal("success", accounting["success_delta"], 0)
    assert_equal("failure", accounting["failure_delta"], 1)
    assert_equal("needs_ingest", accounting["needs_ingest"], False)
    assert_equal("pending_review", result["records_pending_review"], True)
    assert_equal("review_action", result["crawler_review_action"], "review_matched_records")


def test_crawler_accepted_records_are_success_and_need_ingest() -> None:
    result = {
        "returncode": 0,
        "manifest_stats": {"records": 3},
        "topic_validation": {"matched": True, "crawler_review_action": "accept"},
    }
    accounting = apply(result)
    assert_equal("success", accounting["success_delta"], 1)
    assert_equal("needs_ingest", accounting["needs_ingest"], True)
    assert_equal("ingest_deferred", result["ingest_deferred"], "CrawlerAgent explicitly accepted these records; ingest this accepted export after the collection loop finishes.")


def test_empty_matched_artifact_is_not_ingestible_success() -> None:
    result = {"returncode": 0, "manifest_stats": {"records": 1, "usable_records": 0, "empty_records": 1, "record_bytes": 0}, "topic_validation": {"matched": True}}
    accounting = apply(result, source="save_artifact")
    assert_equal("success", accounting["success_delta"], 0)
    assert_equal("failure", accounting["failure_delta"], 1)
    assert_equal("needs_ingest", accounting["needs_ingest"], False)
    assert_equal("pending_review", result["records_pending_review"], True)
    assert_equal("review_action", result["crawler_review_action"], "retry_or_rewrite_empty_artifact")


def test_browser_collect_waits_for_crawler_review() -> None:
    result = {"returncode": 0, "manifest_stats": {"records": 50}}
    accounting = apply(result, source="browser_collect", delivery="human")
    assert_equal("success", accounting["success_delta"], 0)
    assert_equal("failure", accounting["failure_delta"], 1)
    assert_equal("needs_ingest", accounting["needs_ingest"], False)
    assert_equal("pending_review", result["records_pending_review"], True)


def test_browser_collect_manifest_ok_is_structured_success() -> None:
    result = {"returncode": 0, "manifest_stats": {"records": 5, "status": "ok"}}
    accounting = apply(result, source="browser_collect", delivery="human")
    assert_equal("success", accounting["success_delta"], 1)
    assert_equal("failure", accounting["failure_delta"], 0)
    assert_equal("structured_success", result["structured_collection_succeeded"], True)
    assert_equal("ingest_skipped", result["ingest_skipped"], "CrawlerAgent accepted structured browser records for the human-facing task; RAG ingest was not requested.")


def test_browser_collect_accepted_for_human_skips_ingest() -> None:
    result = {"returncode": 0, "manifest_stats": {"records": 50}, "topic_validation": {"matched": True, "crawler_review_action": "accept"}}
    accounting = apply(result, source="browser_collect", delivery="human")
    assert_equal("success", accounting["success_delta"], 1)
    assert_equal("needs_ingest", accounting["needs_ingest"], False)
    assert_equal("ingest_skipped", result["ingest_skipped"], "CrawlerAgent explicitly accepted these records for the human-facing task; RAG ingest was not requested.")


def test_modpack_download_creates_internal_followup() -> None:
    result = {"returncode": 0, "manifest_stats": {"records": 1, "downloads": 1}}
    accounting = apply(result, source="modpack_download")
    assert_equal("success", accounting["success_delta"], 1)
    assert_equal("needs_ingest", accounting["needs_ingest"], True)
    assert_equal("archive_downloaded", result["archive_downloaded"], True)
    assert_equal("ingest_deferred", result["ingest_deferred"], "CrawlerAgent accepted the downloaded archive evidence; ingest the download evidence and then parse internals.")
    assert_equal("followup_source", accounting["followup_task"]["source"], "modpack_internal")


def test_modpack_download_candidate_only_is_not_failure() -> None:
    result = {"returncode": 0, "manifest_stats": {"records": 1, "downloads": 0, "candidates": 1}}
    accounting = apply(result, source="modpack_download")
    assert_equal("candidate", accounting["candidate_delta"], 1)
    assert_equal("failure", accounting["failure_delta"], 0)
    assert_equal("archive_candidate_found", result["archive_candidate_found"], True)


def test_off_topic_records_are_failure() -> None:
    result = {"returncode": 0, "manifest_stats": {"records": 2}, "topic_validation": {"matched": False, "reason": "off_topic"}}
    accounting = apply(result)
    assert_equal("failure", accounting["failure_delta"], 1)
    assert_equal("off_topic", result["off_topic_result"], True)


def test_crawler_review_rejection_records_action() -> None:
    result = {
        "returncode": 0,
        "manifest_stats": {"records": 1},
        "topic_validation": {
            "matched": False,
            "reason": "not_found",
            "cleanup_action": "retry_other_source",
            "next_action": "Search community mirrors instead of reusing this wrong Modrinth URL.",
        },
    }
    accounting = apply(result)
    assert_equal("failure", accounting["failure_delta"], 1)
    assert_equal("off_topic", result["off_topic_result"], True)
    assert_equal("review_action", result["crawler_review_action"], "retry_other_source")
    assert_equal("review_next_action", result["crawler_review_next_action"], "Search community mirrors instead of reusing this wrong Modrinth URL.")


def test_topic_discovery_counts_candidate_only() -> None:
    result = {"returncode": 0, "manifest_stats": {"records": 8}}
    accounting = apply(result, source="topic_discovery")
    assert_equal("candidate", accounting["candidate_delta"], 1)
    assert_equal("candidate_only", result["candidate_only"], True)


def test_mcagent_context_is_diagnostic_and_adds_external_followup() -> None:
    result = {"returncode": 0, "manifest_stats": {"records": 1}}
    accounting = apply(result, source="mcagent_context")
    assert_equal("not_final_success", accounting["success_delta"], 0)
    assert_equal("candidate", accounting["candidate_delta"], 1)
    assert_equal("followup_source", accounting["followup_task"]["source"], "web_discovery")
    assert_equal(
        "ingest_skipped",
        result["ingest_skipped"],
        "MCagent/RAG context is an inter-agent diagnostic artifact; Crawler uses it for planning but does not re-ingest it as new external evidence.",
    )


def test_fetch_url_archive_redirect_adds_modpack_download_followup() -> None:
    result = {
        "returncode": 1,
        "manifest_stats": {
            "records": 0,
            "skipped": 1,
            "archive_url_detected": True,
            "failure_reason": "URL points to a binary modpack archive.",
        },
    }
    accounting = apply(result, source="fetch_url", delivery="MCagent/RAG")
    assert_equal("candidate", accounting["candidate_delta"], 1)
    assert_equal("failure", accounting["failure_delta"], 0)
    assert_equal("needs_ingest", accounting["needs_ingest"], False)
    assert_equal("archive_url_detected", result["archive_url_detected"], True)
    assert_equal("followup_source", accounting["followup_task"]["source"], "modpack_download")


def test_non_numeric_manifest_counts_do_not_break_accounting() -> None:
    result = {
        "returncode": "0",
        "manifest_stats": {
            "records": "unknown",
            "usable_records": "",
            "empty_records": "n/a",
            "downloads": "not-yet",
            "candidates": "1",
            "blockers": None,
        },
    }
    accounting = apply(result, source="modpack_download")
    assert_equal("candidate", accounting["candidate_delta"], 1)
    assert_equal("failure", accounting["failure_delta"], 0)
    assert_equal("archive_candidate_found", result["archive_candidate_found"], True)
    assert_equal("archive_not_downloaded", result["archive_not_downloaded"], True)


if __name__ == "__main__":
    test_matched_records_wait_for_crawler_review()
    test_crawler_accepted_records_are_success_and_need_ingest()
    test_empty_matched_artifact_is_not_ingestible_success()
    test_browser_collect_waits_for_crawler_review()
    test_browser_collect_manifest_ok_is_structured_success()
    test_browser_collect_accepted_for_human_skips_ingest()
    test_modpack_download_creates_internal_followup()
    test_modpack_download_candidate_only_is_not_failure()
    test_off_topic_records_are_failure()
    test_crawler_review_rejection_records_action()
    test_topic_discovery_counts_candidate_only()
    test_mcagent_context_is_diagnostic_and_adds_external_followup()
    test_fetch_url_archive_redirect_adds_modpack_download_followup()
    test_non_numeric_manifest_counts_do_not_break_accounting()
    print("crawler_result_accounting_service_scenarios passed")
