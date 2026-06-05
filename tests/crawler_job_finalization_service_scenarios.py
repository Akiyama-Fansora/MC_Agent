from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.crawler_job_finalization_service import CrawlerJobFinalizationService  # noqa: E402


def assert_equal(name: str, actual, expected) -> None:
    if actual != expected:
        raise AssertionError(f"{name}: expected {expected!r}, got {actual!r}")


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


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
    final = build(
        task_results=[
            {
                "source": "mcmod",
                "query": "农夫乐事",
                "returncode": 0,
                "manifest_stats": {"records": 2},
                "topic_validation": {"matched": True, "reason": "direct"},
                "ingest_deferred": "CrawlerAgent accepted these records; ingest this accepted export after the collection loop finishes.",
            },
            {
                "source": "web_discovery",
                "query": "Farmers Delight unrelated",
                "returncode": 0,
                "manifest_stats": {"records": 1},
                "topic_validation": {"matched": False, "reason": "noise", "next_action": "try exact project page"},
                "off_topic_result": True,
            },
        ]
    )
    assert_equal("status", final["status"], "succeeded")
    assert_equal("error", final["error"], None)
    assert_equal("ingest_background", final["result"]["ingest_background"], True)
    assert_equal("ingest_status", final["result"]["loop"][3]["status"], "running")
    audit = final["result"]["self_audit"]
    assert_equal("audit_accepted", audit["counts"]["accepted"], 1)
    assert_equal("audit_rejected", audit["counts"]["rejected"], 1)
    assert_equal("audit_ingest_status", audit["ingest_status"], "running")
    assert_equal("audit_review_summary", audit["review_summary"], "accepted=1; rejected=1; pending_review=0; ingest=running")
    assert_true("audit_accepted_note", "Accepted by CrawlerAgent" in audit["accepted_sources"][0]["review_note"])
    assert_equal("audit_rejected_reason", audit["rejected_sources"][0]["rejected_reason"], "noise")
    assert_true("audit_rejected_note", "Rejected by CrawlerAgent" in audit["rejected_sources"][0]["review_note"])
    assert_equal("audit_objective_records", audit["accepted_sources"][0]["objective_evidence"]["records"], 2)


def test_failed_without_success_reports_all_sources_failed() -> None:
    final = build(success_count=0, needs_ingest=False)
    assert_equal("status", final["status"], "failed")
    assert_equal("error", final["error"], "all crawler sources failed")
    assert_equal("verify_status", final["result"]["loop"][4]["status"], "failed")


def test_candidate_only_probe_can_succeed_without_ingest() -> None:
    final = build(
        success_count=0,
        candidate_count=2,
        failure_count=0,
        needs_ingest=False,
        task_results=[
            {"source": "mcagent_context", "returncode": 0, "manifest_stats": {"records": 1}},
            {"source": "modpack_download", "returncode": 0, "manifest_stats": {"records": 2, "candidates": 1}},
        ],
        plan={"topic": "乌托邦整合包", "agent_finish_reason": "gap probe complete"},
    )
    assert_equal("status", final["status"], "succeeded")
    assert_equal("error", final["error"], None)
    assert_equal("verify_status", final["result"]["loop"][4]["status"], "done")


def test_gap_probe_candidate_can_succeed_with_rejected_source() -> None:
    final = build(
        success_count=0,
        candidate_count=1,
        failure_count=1,
        needs_ingest=False,
        task_results=[
            {"source": "mcagent_context", "returncode": 0, "manifest_stats": {"records": 1}},
            {"source": "web_discovery", "returncode": 0, "manifest_stats": {"records": 0}, "empty_result": True},
        ],
        plan={
            "topic": "Utopian Journey",
            "agent_finish_reason": "MCagent/RAG returned usable local context with no explicit gap list, and Crawler collected at least one external probe/candidate; finish this inter-agent gap check instead of expanding into a full crawl.",
        },
    )
    assert_equal("status", final["status"], "succeeded")
    assert_equal("error", final["error"], None)
    assert_equal("verify_status", final["result"]["loop"][4]["status"], "done")


def test_context_checkpoint_candidate_can_succeed_with_rejected_sources() -> None:
    final = build(
        success_count=0,
        candidate_count=1,
        failure_count=3,
        needs_ingest=False,
        task_results=[
            {"source": "mcagent_context", "returncode": 0, "manifest_stats": {"records": 1}},
            {"source": "web_discovery", "returncode": 0, "manifest_stats": {"records": 1}, "off_topic_result": True},
        ],
        plan={
            "topic": "Utopian Journey",
            "agent_finish_reason": "Crawler has received MCagent/RAG context and found at least one external candidate or accepted source; recent follow-up tools are low-yield, so finish with the usable material and remaining gaps instead of exhausting slow browser tasks.",
        },
    )
    assert_equal("status", final["status"], "succeeded")
    assert_equal("error", final["error"], None)
    assert_equal("verify_status", final["result"]["loop"][4]["status"], "done")


def test_reflection_timeout_with_candidate_evidence_is_partial_success() -> None:
    final = build(
        success_count=0,
        candidate_count=1,
        failure_count=2,
        needs_ingest=False,
        task_results=[
            {"source": "mcagent_context", "returncode": 0, "manifest_stats": {"records": 1}},
            {"source": "web_discovery", "returncode": 0, "manifest_stats": {"records": 1}, "records_pending_review": True},
            {"source": "web_discovery", "returncode": 0, "manifest_stats": {"records": 0}, "empty_result": True},
        ],
        plan={
            "topic": "Utopian Journey",
            "agent_finish_reason": "CrawlerAgent reflection timed out after 90s after repeated empty/off-topic observations; finish with the objective evidence already collected instead of executing more slow pending tasks blindly.",
        },
    )
    assert_equal("status", final["status"], "succeeded")
    assert_equal("error", final["error"], None)
    assert_equal("verify_status", final["result"]["loop"][4]["status"], "done")


def test_finish_reflection_reason_is_used_when_plan_finish_reason_missing() -> None:
    final = build(
        success_count=0,
        candidate_count=1,
        failure_count=1,
        needs_ingest=False,
        task_results=[
            {"source": "mcagent_context", "returncode": 0, "manifest_stats": {"records": 1}},
            {"source": "web_discovery", "returncode": 0, "manifest_stats": {"records": 1}, "records_pending_review": True},
        ],
        plan={
            "topic": "Utopian Journey",
            "agent_reflections": [
                {
                    "action": "finish",
                    "planner": "runtime_reflection_timeout",
                    "reason": "CrawlerAgent reflection timed out after 90s after repeated empty/off-topic observations; finish with the objective evidence already collected instead of executing more slow pending tasks blindly.",
                }
            ],
        },
    )
    assert_equal("status", final["status"], "succeeded")
    assert_equal("error", final["error"], None)


def test_reflection_timeout_reason_survives_low_yield_done_summary() -> None:
    final = build(
        success_count=0,
        candidate_count=1,
        failure_count=2,
        needs_ingest=False,
        task_results=[
            {"source": "mcagent_context", "returncode": 0, "manifest_stats": {"records": 1}},
            {"source": "web_discovery", "returncode": 0, "manifest_stats": {"records": 0, "candidates": 12}, "empty_result": True},
            {"source": "web_discovery", "returncode": 0, "manifest_stats": {"records": 0, "candidates": 1}, "empty_result": True},
        ],
        plan={
            "topic": "Utopian Journey",
            "agent_reflections": [
                {
                    "action": "finish",
                    "planner": "runtime_reflection_timeout",
                    "reason": "CrawlerAgent reflection timed out after 90s after repeated empty/off-topic observations; finish with the objective evidence already collected instead of executing more slow pending tasks blindly.",
                    "done_summary": "CrawlerAgent stopped after repeated low-yield observations and a reflection timeout.",
                }
            ],
        },
    )
    assert_equal("status", final["status"], "succeeded")
    assert_equal("error", final["error"], None)
    assert_equal("verify_status", final["result"]["loop"][4]["status"], "done")


def test_stopped_job_keeps_error_clear() -> None:
    final = build(stop_requested=True, success_count=0, needs_ingest=True, task_results=[{}, {}], planned_tasks=[{}, {}, {}])
    assert_equal("status", final["status"], "stopped")
    assert_equal("error", final["error"], None)
    assert_equal("ingest_background", final["result"]["ingest_background"], False)
    assert "已完成 2/3" in final["summary"]


def test_reflection_failure_before_tools_is_reported_explicitly() -> None:
    reason = "CrawlerAgent could not review objective results because the reflection LLM failed."
    final = build(success_count=0, needs_ingest=False, task_results=[], planned_tasks=[{"source": "web_discovery"}], plan={"topic": "Utopia", "agent_finish_reason": reason})
    assert_equal("status", final["status"], "failed")
    assert_equal("error", final["error"], reason)
    assert_true("summary_mentions_stopped", "stopped before tool execution" in final["summary"])
    assert_equal("act_blocked", final["result"]["loop"][2]["status"], "blocked")


def test_finalization_moves_unqualified_modpack_internal_out_of_planned_tasks() -> None:
    final = build(
        planned_tasks=[
            {"source": "web_discovery", "query": "乌托邦探险之旅 攻略"},
            {"source": "modpack_internal", "query": "Utopian Journey"},
            {"source": "modpack_internal", "query": "Utopian Journey", "archive_path": "D:\\packs\\utopia.mrpack"},
        ]
    )
    sources = [task["source"] for task in final["result"]["planned_tasks"]]
    assert_equal("planned_sources", sources, ["web_discovery", "modpack_internal"])
    assert_equal("blocked_count", len(final["result"]["blocked_planned_tasks"]), 1)
    assert_equal("blocked_reason", final["result"]["blocked_planned_tasks"][0]["blocked_reason"], "requires_any:zip|archive|archive_path|manifest_path|path")


if __name__ == "__main__":
    test_success_with_ingest_builds_running_ingest_loop()
    test_failed_without_success_reports_all_sources_failed()
    test_candidate_only_probe_can_succeed_without_ingest()
    test_gap_probe_candidate_can_succeed_with_rejected_source()
    test_context_checkpoint_candidate_can_succeed_with_rejected_sources()
    test_reflection_timeout_with_candidate_evidence_is_partial_success()
    test_finish_reflection_reason_is_used_when_plan_finish_reason_missing()
    test_reflection_timeout_reason_survives_low_yield_done_summary()
    test_stopped_job_keeps_error_clear()
    test_reflection_failure_before_tools_is_reported_explicitly()
    test_finalization_moves_unqualified_modpack_internal_out_of_planned_tasks()
    print("crawler_job_finalization_service_scenarios passed")
