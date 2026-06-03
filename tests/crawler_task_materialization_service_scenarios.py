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


def test_materialize_replan_tasks_blocks_modpack_internal_without_archive_input() -> None:
    service = CrawlerTaskMaterializationService()
    tasks = service.materialize_replan_tasks(
        new_plan={
            "tasks": [
                {"source": "modpack_internal", "query": "Utopian Journey", "reason": "parse pack internals"},
                {"source": "modpack_download", "query": "Utopian Journey", "reason": "find public archive"},
            ]
        },
        existing_tasks=[],
        identity_fn=identity,
        source_alias_fn=source_alias,
        max_new_tasks=3,
    )
    assert_equal("count", len(tasks), 1)
    assert_equal("source", tasks[0]["source"], "modpack_download")


def test_reflection_task_filter_allows_modpack_internal_with_archive_path() -> None:
    service = CrawlerTaskMaterializationService()
    executable, blocked = service.filter_executable_reflection_tasks(
        [
            {"source": "modpack_internal", "query": "Utopian Journey"},
            {"source": "modpack_internal", "query": "Utopian Journey", "archive_path": "D:\\packs\\utopia.mrpack"},
            {"source": "web_discovery", "query": "Utopian Journey"},
            {"source": "fetch_url", "query": "Python requests docs"},
            {"source": "save_artifact", "query": "save this"},
        ]
    )
    assert_equal("blocked_count", len(blocked), 3)
    assert_equal("executable_count", len(executable), 2)
    assert_equal("archive_task_source", executable[0]["source"], "modpack_internal")
    blocked_reasons = " ".join(str(task.get("blocked_reason") or "") for task in blocked)
    assert "url_required" in blocked_reasons
    assert "requires_any:content|content_ref|artifact_ref" in blocked_reasons


def test_displayable_planned_tasks_split_blocks_modpack_internal_without_archive_input() -> None:
    service = CrawlerTaskMaterializationService()
    displayable, blocked = service.split_displayable_planned_tasks(
        [
            {"source": "web_discovery", "query": "乌托邦探险之旅 攻略"},
            {"source": "modpack_internal", "query": "Utopian Journey"},
            {"source": "modpack_internal", "query": "Utopian Journey", "archive_path": "D:\\packs\\utopia.mrpack"},
        ]
    )
    assert_equal("displayable_sources", [task["source"] for task in displayable], ["web_discovery", "modpack_internal"])
    assert_equal("blocked_count", len(blocked), 1)
    assert_equal("blocked_reason", blocked[0]["blocked_reason"], "requires_any:zip|archive|archive_path|manifest_path|path")


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
    test_materialize_replan_tasks_blocks_modpack_internal_without_archive_input()
    test_reflection_task_filter_allows_modpack_internal_with_archive_path()
    test_displayable_planned_tasks_split_blocks_modpack_internal_without_archive_input()
    test_record_replan_appends_observable_history()
    test_topic_review_materialization_filters_discovery_and_duplicates()
    test_fallback_topic_tasks_switches_sources_after_first_ten()
    print("crawler_task_materialization_service_scenarios passed")
