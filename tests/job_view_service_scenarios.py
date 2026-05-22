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
    return {"mcmod": "MC百科搜索", "firecrawl": "Firecrawl", "jina": "Jina Reader"}.get(source, source)


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
                {"source": "firecrawl", "query": "乌托邦探险之旅 玩法", "reason": "教程页"},
            ],
            "tasks": [
                {"source": "mcmod", "query": "乌托邦探险之旅", "returncode": 0, "ingest_deferred": True, "manifest_stats": {"records": 2}},
                {"source": "firecrawl", "query": "乌托邦探险之旅 玩法", "returncode": 1, "output": "HTTP 429 quota exceeded"},
            ],
        },
    }

    view = JobReadableViewService(source_label=label).build(job)
    assert_equal("headline", view["headline"], "乌托邦探险之旅 · 运行中")
    assert_equal("status_label", view["status_label"], "运行中")
    assert_equal("target", view["target"], "乌托邦探险之旅")
    assert_equal("current_index", view["current_index"], 2)
    assert_equal("total_tasks", view["total_tasks"], 2)
    assert_equal("progress_text", view["progress_text"], "第 2 / 2 个采集动作")
    assert_equal("current_source", view["current_source"], "Firecrawl")
    assert_equal("observation_ok", view["observation_statuses"].get("ok"), 1)
    assert_equal("observation_quota", view["observation_statuses"].get("quota_limited"), 1)
    assert_true("health_quota", "额度不足" in view["health_text"])
    assert_true("next_action", "Firecrawl" in view["next_action"])


def test_waiting_job_view_is_plain_language() -> None:
    view = JobReadableViewService(source_label=label).build({"title": "Crawler 采集任务", "status": "running", "result": {}})
    assert_equal("headline", view["headline"], "Crawler 采集任务 · 运行中")
    assert_equal("progress_text", view["progress_text"], "等待 Crawler 规划任务")
    assert_equal("health_text", view["health_text"], "Crawler 正在规划或刚开始执行。")
    assert_equal("next_action", view["next_action"], "等待 Crawler 规划任务。")


if __name__ == "__main__":
    test_running_job_view_explains_current_action_and_counts()
    test_waiting_job_view_is_plain_language()
    print("job_view_service_scenarios: ok")
