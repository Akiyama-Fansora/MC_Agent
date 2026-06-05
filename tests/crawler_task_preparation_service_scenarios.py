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


def test_browser_collect_recovers_url_path_and_count_from_original_question() -> None:
    question = (
        "\u7528 Crawler \u6253\u5f00 https://webscraper.io/test-sites/e-commerce/static/computers/laptops "
        "\u63d0\u53d6\u524d 5 \u4e2a\u5546\u54c1\u7684\u540d\u79f0\u3001\u4ef7\u683c\u3001\u94fe\u63a5\uff0c"
        "\u4fdd\u5b58\u4e3a xlsx\u3001csv\u3001json \u5230 D:\\magic\\MC_Agent\\data\\manual_tests\\items\u3002"
    )
    built = CrawlerTaskPreparationService().build_payload(
        base_payload={"original_user_request": question},
        task={"query": "\u4ece\u6307\u5b9a\u7535\u5546\u6d4b\u8bd5\u9875\u9762\u6293\u53d6\u524d\u4e94\u4e2a\u7b14\u8bb0\u672c\u5546\u54c1\u7684\u57fa\u672c\u4fe1\u606f"},
        question=question,
        task_source="browser_collect",
    )
    assert_equal("start_url", built["start_url"], "https://webscraper.io/test-sites/e-commerce/static/computers/laptops")
    assert_equal("output_dir", built["output_dir"], r"D:\magic\MC_Agent\data\manual_tests\items")
    assert_equal("max_items", built["max_items"], 5)


def test_empty_query_result_is_objective_failure_observation() -> None:
    result = CrawlerTaskPreparationService().empty_query_result(
        task_source="fetch_url",
        task={"reason": "LLM returned an empty query"},
    )
    assert_equal("returncode", result["returncode"], 2)
    assert_equal("empty_result", result["empty_result"], True)
    assert_equal("reason", result["reason"], "LLM returned an empty query")
    assert_equal("observation_status", result["observation"]["status"], "blocked")
    assert_equal("retryable", result["observation"]["retryable"], True)


def test_blocked_preflight_result_is_returned_to_crawler_for_reflection() -> None:
    result = CrawlerTaskPreparationService().blocked_preflight_result(
        task_source="fetch_url",
        task={"query": "Python requests docs", "reason": "LLM selected fetch_url before discovering a URL"},
        context_text="Collect Python requests official docs.",
    )
    assert result is not None
    assert_equal("returncode", result["returncode"], 2)
    assert_equal("source", result["source"], "fetch_url")
    assert_equal("preflight_valid", result["capability_preflight"]["valid"], False)
    assert "url_required" in result["capability_preflight"]["issues"]
    assert_equal("observation_status", result["observation"]["status"], "empty")


def test_local_search_output_dir_is_not_a_search_root() -> None:
    result = CrawlerTaskPreparationService().blocked_preflight_result(
        task_source="search_local_files",
        task={"query": "Farmer's Delight", "output_dir": r"D:\tmp\crawler-output", "reason": "LLM confused save destination with input root"},
        context_text="Collect Farmer's Delight public web evidence for MCagent/RAG.",
    )
    assert result is not None
    assert_equal("returncode", result["returncode"], 2)
    assert_equal("preflight_valid", result["capability_preflight"]["valid"], False)
    assert "requires_any:path|root" in result["capability_preflight"]["issues"]


if __name__ == "__main__":
    test_build_payload_preserves_task_specific_fields()
    test_build_payload_falls_back_to_question_when_query_missing()
    test_build_payload_preserves_generic_artifact_fields()
    test_browser_collect_recovers_url_path_and_count_from_original_question()
    test_empty_query_result_is_objective_failure_observation()
    test_blocked_preflight_result_is_returned_to_crawler_for_reflection()
    test_local_search_output_dir_is_not_a_search_root()
    print("crawler_task_preparation_service_scenarios passed")

