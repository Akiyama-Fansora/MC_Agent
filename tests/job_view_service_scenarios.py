from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.job_view_service import JobReadableViewService  # noqa: E402


def assert_equal(name: str, actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"{name}: expected {expected!r}, got {actual!r}")


def assert_true(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name}: {detail}")


def label(source: str) -> str:
    return {"mcmod": "MC百科搜索", "playwright": "Playwright", "fetch_url": "Local URL Fetch"}.get(source, source)


def test_running_job_view_explains_current_action_and_counts() -> None:
    job = {
        "title": "Crawler 多源补库 -> RAG",
        "status": "running",
        "summary": "running",
        "result": {
            "plan": {
                "topic": "乌托邦探险之旅",
                "delivery_target": "MCagent/RAG",
                "coverage_goals": ["完整模组列表", "玩法机制"],
                "agent_reflections": [{"action": "continue", "reason": "MC百科结果有效，继续找玩法教程", "planner": "test"}],
            },
            "planned_tasks": [
                {"source": "mcmod", "query": "乌托邦探险之旅", "reason": "项目页"},
                {"source": "playwright", "query": "乌托邦探险之旅 玩法", "reason": "教程页"},
            ],
            "tasks": [
                {
                    "source": "mcmod",
                    "query": "乌托邦探险之旅",
                    "returncode": 0,
                    "ingest_deferred": True,
                    "manifest_stats": {"records": 2},
                    "topic_validation": {"matched": True, "crawler_review_action": "accept"},
                },
                {"source": "playwright", "query": "乌托邦探险之旅 玩法", "returncode": 1, "output": "HTTP 429 quota exceeded"},
            ],
            "replan_count": 1,
            "ingest_background": True,
        },
    }

    view = JobReadableViewService(source_label=label).build(job)
    assert_equal("headline", view["headline"], "乌托邦探险之旅 · 运行中")
    assert_equal("status_label", view["status_label"], "运行中")
    assert_equal("target", view["target"], "乌托邦探险之旅")
    assert_equal("current_index", view["current_index"], 2)
    assert_equal("total_tasks", view["total_tasks"], 2)
    assert_equal("progress_text", view["progress_text"], "第 2 / 2 个采集动作")
    assert_equal("current_source", view["current_source"], "Playwright")
    assert_equal("observation_ok", view["observation_statuses"].get("ok"), 1)
    assert_equal("observation_quota", view["observation_statuses"].get("quota_limited"), 1)
    assert_true("health_quota", "额度不足" in view["health_text"])
    assert_true("next_action", "Playwright" in view["next_action"])
    assert_equal("timeline_first", view["timeline"][0]["type"], "plan")
    assert_true("timeline_task", any(item["type"] == "task" and item["status"] == "quota_limited" for item in view["timeline"]))
    assert_true("timeline_reflection", any(item["type"] == "reflection" for item in view["timeline"]))
    assert_true("timeline_replan", any(item["type"] == "replan" for item in view["timeline"]))
    assert_true("timeline_ingest", any(item["type"] == "ingest" and item["status"] == "running" for item in view["timeline"]))
    assert_equal("self_audit_accepted", view["self_audit"]["counts"]["accepted"], 1)
    assert_equal("self_audit_rejected", view["self_audit"]["counts"]["rejected"], 1)
    assert_true("self_audit_summary", "接受 1 个来源" in view["self_audit_summary"])
    assert_true("accepted_source_decision", view["self_audit"]["accepted_sources"][0]["review_note"].startswith("Accepted by CrawlerAgent"))
    assert_true("rejected_source_decision", view["self_audit"]["rejected_sources"][0]["review_note"].startswith("Rejected by CrawlerAgent"))
    assert_true("source_decisions_visible", any(item["decision"] == "accepted" for item in view["self_audit"]["source_decisions"]))


def test_waiting_job_view_is_plain_language() -> None:
    view = JobReadableViewService(source_label=label).build({"title": "Crawler 采集任务", "status": "running", "result": {}})
    assert_equal("headline", view["headline"], "Crawler 采集任务 · 运行中")
    assert_equal("progress_text", view["progress_text"], "等待 Crawler 规划任务")
    assert_equal("health_text", view["health_text"], "Crawler 正在规划或刚开始执行。")
    assert_equal("next_action", view["next_action"], "等待 Crawler 规划任务。")
    assert_equal("timeline", view["timeline"], [])


def test_job_view_surfaces_blocked_planned_tasks_separately() -> None:
    view = JobReadableViewService(source_label=label).build(
        {
            "title": "Crawler 采集任务",
            "status": "succeeded",
            "result": {
                "plan": {"topic": "乌托邦探险之旅"},
                "planned_tasks": [{"source": "web_discovery", "query": "乌托邦探险之旅 攻略"}],
                "blocked_planned_tasks": [
                    {
                        "source": "modpack_internal",
                        "query": "Utopian Journey",
                        "blocked_reason": "modpack_internal_requires_archive_path",
                    }
                ],
                "tasks": [],
            },
        }
    )
    assert_equal("total_tasks", view["total_tasks"], 1)
    assert_equal("blocked_count", len(view["blocked_planned_tasks"]), 1)
    assert_equal("blocked_reason", view["blocked_planned_tasks"][0]["blocked_reason"], "modpack_internal_requires_archive_path")
    assert_true("timeline_no_blocked_as_task", all(item.get("title") != "modpack_internal" for item in view["timeline"]))


def test_job_view_recomputes_self_audit_after_background_ingest_finishes() -> None:
    view = JobReadableViewService(source_label=label).build(
        {
            "title": "Crawler 采集",
            "status": "succeeded",
            "summary": "后台入库已完成。",
            "result": {
                "ingest": {"stats": {"documents_loaded": 2}},
                "ingest_background": True,
                "self_audit": {"ingest_status": "running", "ingest_note": "stale cached audit"},
                "tasks": [
                    {
                        "source": "fetch_url",
                        "query": "https://example.test/project",
                        "returncode": 0,
                        "ingest_deferred": True,
                        "manifest_stats": {"records": 1},
                        "topic_validation": {"matched": True, "reason": "direct"},
                    }
                ],
            },
        }
    )
    assert_equal("audit_ingest_done", view["self_audit"]["ingest_status"], "done")
    assert_true("audit_summary_done", "已入库" in view["self_audit_summary"], view["self_audit_summary"])


def test_zero_byte_accepted_result_is_not_shown_as_useful_output() -> None:
    view = JobReadableViewService(source_label=label).build(
        {
            "title": "Crawler 采集",
            "status": "succeeded",
            "result": {
                "tasks": [
                    {
                        "source": "save_artifact",
                        "query": "Farmer's Delight summary",
                        "returncode": 0,
                        "manifest_stats": {"records": 1, "usable_records": 0, "empty_records": 1},
                        "observation": {"status": "records_pending_review", "summary": "empty artifact"},
                        "records_pending_review": True,
                    }
                ],
            },
        }
    )
    assert_equal("useful_outputs", view["useful_outputs"], [])
    assert_equal("blocked_outputs", len(view["blocked_outputs"]), 1)


def test_non_numeric_manifest_stats_do_not_break_job_view() -> None:
    view = JobReadableViewService(source_label=label).build(
        {
            "title": "Crawler collection",
            "status": "succeeded",
            "result": {
                "tasks": [
                    {
                        "source": "fetch_url",
                        "query": "https://example.test/project",
                        "returncode": 0,
                        "manifest_stats": {
                            "records": "unknown",
                            "usable_records": "n/a",
                            "empty_records": "",
                            "record_bytes": "pending",
                            "skipped": None,
                            "errors": "not-yet",
                        },
                        "observation": {"status": "ok", "summary": "tool returned partial stats"},
                        "topic_validation": {"matched": True, "crawler_review_action": "accept"},
                    }
                ],
            },
        }
    )
    assert_equal("useful_outputs", len(view["useful_outputs"]), 1)
    assert_equal("audit_records", view["self_audit"]["accepted_sources"][0]["records"], 0)
    assert_equal("audit_usable_records", view["self_audit"]["accepted_sources"][0]["usable_records"], 0)
    assert_equal("objective_records", view["self_audit"]["accepted_sources"][0]["objective_evidence"]["records"], 0)


def test_non_numeric_returncode_without_observation_does_not_break_job_view() -> None:
    view = JobReadableViewService(source_label=label).build(
        {
            "title": "Crawler collection",
            "status": "succeeded",
            "result": {
                "tasks": [
                    {
                        "source": "fetch_url",
                        "query": "https://example.test/project",
                        "returncode": "unknown",
                        "manifest_stats": {"records": 1},
                        "output": "partial tool metadata",
                    }
                ],
            },
        }
    )
    assert_equal("observation_pending", view["observation_statuses"].get("records_pending_review"), 1)
    assert_equal("self_audit_pending", view["self_audit"]["counts"]["pending_review"], 1)
    assert_equal("useful_outputs", view["useful_outputs"], [])


if __name__ == "__main__":
    test_running_job_view_explains_current_action_and_counts()
    test_waiting_job_view_is_plain_language()
    test_job_view_surfaces_blocked_planned_tasks_separately()
    test_job_view_recomputes_self_audit_after_background_ingest_finishes()
    test_zero_byte_accepted_result_is_not_shown_as_useful_output()
    test_non_numeric_manifest_stats_do_not_break_job_view()
    test_non_numeric_returncode_without_observation_does_not_break_job_view()
    print("job_view_service_scenarios: ok")
