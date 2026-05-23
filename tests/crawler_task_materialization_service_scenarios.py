from __future__ import annotations

from pathlib import Path
import re
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.crawler_task_materialization_service import CrawlerTaskMaterializationService  # noqa: E402


def identity(task: dict[str, Any]) -> tuple[str, str]:
    source = str(task.get("source") or "").strip().lower()
    query = re.sub(r"\s+", " ", str(task.get("query") or "").strip()).lower()
    return source, query


def source_alias(value: str) -> str:
    return {"web": "web_discovery", "mc百科": "mcmod"}.get(value, value)


def assert_equal(name: str, actual, expected) -> None:
    if actual != expected:
        raise AssertionError(f"{name}: expected {expected!r}, got {actual!r}")


def test_replan_session_summary_keeps_goal_and_existing_tasks() -> None:
    service = CrawlerTaskMaterializationService()
    summary = service.replan_session_summary(
        question="乌托邦整合包",
        plan={"topic": "乌托邦探险之旅", "delivery_target": "MCagent/RAG", "coverage_goals": ["玩法"]},
        failure_summary=[{"source": "fetch_url", "reason": "empty"}],
        existing_tasks=[{"source": "fetch_url", "query": "乌托邦"}],
        identity_fn=identity,
    )
    assert_equal("mode", summary["mode"], "mid_job_replan")
    assert_equal("previous_topic", summary["previous_topic"], "乌托邦探险之旅")
    assert_equal("existing", summary["already_planned_tasks"], [{"source": "fetch_url", "query": "乌托邦"}])


def test_materialize_replan_tasks_normalizes_and_deduplicates() -> None:
    service = CrawlerTaskMaterializationService()
    tasks = service.materialize_replan_tasks(
        new_plan={
            "tasks": [
                {"source": "fetch_url", "query": "乌托邦", "reason": "duplicate"},
                {"source": "web", "query": "乌托邦 探险之旅 boss", "reason": "new route"},
                {"source": "mcmod", "query": ""},
            ]
        },
        existing_tasks=[{"source": "fetch_url", "query": "乌托邦"}],
        identity_fn=identity,
        source_alias_fn=source_alias,
        max_new_tasks=3,
    )
    assert_equal("count", len(tasks), 1)
    assert_equal("source", tasks[0]["source"], "web_discovery")
    assert "mid-job replan" in tasks[0]["reason"]


def test_record_replan_appends_observable_history() -> None:
    service = CrawlerTaskMaterializationService()
    plan: dict[str, Any] = {}
    service.record_replan(
        plan=plan,
        task_results_count=2,
        failure_summary=[{"reason": "off_topic"}],
        new_tasks=[{"source": "mcmod", "query": "落幕曲"}],
        new_plan={"raw_plan": {"_planner_model": "deepseek"}},
    )
    assert_equal("history_count", len(plan["replans"]), 1)
    assert_equal("planner", plan["replans"][0]["planner"], "deepseek")


def test_topic_review_materialization_filters_discovery_and_duplicates() -> None:
    service = CrawlerTaskMaterializationService()
    tasks = service.materialize_topic_review_tasks(
        review_plan={
            "tasks": [
                {"source": "topic_discovery", "query": "内部候选"},
                {"source": "mc百科", "query": "乌托邦 模组列表", "reason": "project page"},
                {"source": "mcmod", "query": "已有"},
            ]
        },
        existing_tasks=[{"source": "mcmod", "query": "已有"}],
        identity_fn=identity,
        source_alias_fn=source_alias,
        max_new_tasks=2,
    )
    assert_equal("count", len(tasks), 1)
    assert_equal("source_alias", tasks[0]["source"], "mcmod")
    assert "Crawler LLM reviewed" in tasks[0]["reason"]


def test_fallback_topic_tasks_switches_sources_after_first_ten() -> None:
    service = CrawlerTaskMaterializationService()
    seeds = [f"query {index}" for index in range(12)]
    tasks = service.fallback_topic_tasks(seed_queries=seeds, existing_tasks=[{"source": "mcmod", "query": "query 0"}], identity_fn=identity, max_new_tasks=12)
    assert_equal("deduped_count", len(tasks), 11)
    assert_equal("first_after_duplicate", tasks[0]["source"], "mcmod")
    assert_equal("web_discovery_after_ten", tasks[-1]["source"], "web_discovery")
    assert_equal("max_urls", tasks[-1]["max_urls"], 6)


if __name__ == "__main__":
    test_replan_session_summary_keeps_goal_and_existing_tasks()
    test_materialize_replan_tasks_normalizes_and_deduplicates()
    test_record_replan_appends_observable_history()
    test_topic_review_materialization_filters_discovery_and_duplicates()
    test_fallback_topic_tasks_switches_sources_after_first_ten()
    print("crawler_task_materialization_service_scenarios passed")

