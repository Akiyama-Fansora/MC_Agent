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
                {"source": "mcmod", "query": "乌托邦探险之旅", "returncode": 0, "ingest_deferred": True, "manifest_stats": {"records": 2}, "topic_validation": {"matched": True}},
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


if __name__ == "__main__":
    test_running_job_view_explains_current_action_and_counts()
    test_waiting_job_view_is_plain_language()
    test_job_view_surfaces_blocked_planned_tasks_separately()
    print("job_view_service_scenarios: ok")

