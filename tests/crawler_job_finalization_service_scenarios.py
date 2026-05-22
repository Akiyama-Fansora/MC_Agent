from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.crawler_job_finalization_service import CrawlerJobFinalizationService  # noqa: E402


def assert_equal(name: str, actual, expected) -> None:
    if actual != expected:
        raise AssertionError(f"{name}: expected {expected!r}, got {actual!r}")


def build(**overrides):
    payload = {
        "stop_requested": False,
        "success_count": 1,
        "candidate_count": 0,
        "failure_count": 2,
        "replan_count": 1,
        "needs_ingest": True,
        "task_results": [{"source": "mcmod"}],
        "planned_tasks": [{"source": "mcmod"}],
        "plan": {"topic": "落幕曲"},
        "collection_summary": {"totals": {"records": 1}},
    }
    payload.update(overrides)
    return CrawlerJobFinalizationService().build(**payload)


def test_success_with_ingest_builds_running_ingest_loop() -> None:
    final = build()
    assert_equal("status", final["status"], "succeeded")
    assert_equal("error", final["error"], None)
    assert_equal("ingest_background", final["result"]["ingest_background"], True)
    assert_equal("ingest_status", final["result"]["loop"][3]["status"], "running")


def test_failed_without_success_reports_all_sources_failed() -> None:
    final = build(success_count=0, needs_ingest=False)
    assert_equal("status", final["status"], "failed")
    assert_equal("error", final["error"], "all crawler sources failed")
    assert_equal("verify_status", final["result"]["loop"][4]["status"], "failed")


def test_stopped_job_keeps_error_clear() -> None:
    final = build(stop_requested=True, success_count=0, needs_ingest=True, task_results=[{}, {}], planned_tasks=[{}, {}, {}])
    assert_equal("status", final["status"], "stopped")
    assert_equal("error", final["error"], None)
    assert_equal("ingest_background", final["result"]["ingest_background"], False)
    assert "已完成 2/3" in final["summary"]


if __name__ == "__main__":
    test_success_with_ingest_builds_running_ingest_loop()
    test_failed_without_success_reports_all_sources_failed()
    test_stopped_job_keeps_error_clear()
    print("crawler_job_finalization_service_scenarios passed")
