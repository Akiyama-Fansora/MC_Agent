from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.crawler_task_preparation_service import CrawlerTaskPreparationService  # noqa: E402


def assert_equal(name: str, actual, expected) -> None:
    if actual != expected:
        raise AssertionError(f"{name}: expected {expected!r}, got {actual!r}")


def test_build_payload_preserves_task_specific_fields() -> None:
    payload = {"delivery_target": "MCagent/RAG", "source": "planner"}
    task = {
        "query": "乌托邦探险之旅",
        "reason": "render page",
        "search_limit": 8,
        "fields": ["name", "price", "url"],
        "output_dir": r"C:\tmp\items",
    }
    built = CrawlerTaskPreparationService().build_payload(
        base_payload=payload,
        task=task,
        question="补全乌托邦资料",
        task_source="browser_collect",
    )
    assert_equal("source", built["source"], "browser_collect")
    assert_equal("query", built["query"], "乌托邦探险之旅")
    assert_equal("question", built["question"], "补全乌托邦资料")
    assert_equal("delivery_target", built["delivery_target"], "MCagent/RAG")
    assert_equal("fields", built["fields"], ["name", "price", "url"])
    assert_equal("output_dir", built["output_dir"], r"C:\tmp\items")


def test_build_payload_falls_back_to_question_when_query_missing() -> None:
    built = CrawlerTaskPreparationService().build_payload(
        base_payload={},
        task={"reason": "single source"},
        question="落幕曲 Boss 列表",
        task_source="mcmod",
    )
    assert_equal("query", built["query"], "落幕曲 Boss 列表")


def test_build_payload_preserves_generic_artifact_fields() -> None:
    built = CrawlerTaskPreparationService().build_payload(
        base_payload={},
        task={
            "query": "save summary",
            "content": [{"name": "item", "price": 12}],
            "format": "csv",
            "path": r"C:\tmp\items",
            "filename": "items.csv",
            "overwrite": False,
            "metadata": {"source": "test"},
            "content_ref": "latest:md",
        },
        question="save collected data",
        task_source="save_artifact",
    )
    assert_equal("source", built["source"], "save_artifact")
    assert_equal("content", built["content"], [{"name": "item", "price": 12}])
    assert_equal("format", built["format"], "csv")
    assert_equal("path", built["path"], r"C:\tmp\items")
    assert_equal("filename", built["filename"], "items.csv")
    assert_equal("metadata", built["metadata"], {"source": "test"})
    assert_equal("content_ref", built["content_ref"], "latest:md")


def test_empty_query_result_is_objective_failure_observation() -> None:
    result = CrawlerTaskPreparationService().empty_query_result(
        task_source="jina",
        task={"reason": "LLM returned an empty query"},
    )
    assert_equal("returncode", result["returncode"], 2)
    assert_equal("empty_result", result["empty_result"], True)
    assert_equal("reason", result["reason"], "LLM returned an empty query")
    assert_equal("observation_status", result["observation"]["status"], "blocked")
    assert_equal("retryable", result["observation"]["retryable"], True)


if __name__ == "__main__":
    test_build_payload_preserves_task_specific_fields()
    test_build_payload_falls_back_to_question_when_query_missing()
    test_build_payload_preserves_generic_artifact_fields()
    test_empty_query_result_is_objective_failure_observation()
    print("crawler_task_preparation_service_scenarios passed")
