from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.crawler_temporary_extract_service import CrawlerTemporaryExtractService  # noqa: E402


def assert_equal(name: str, actual, expected) -> None:
    if actual != expected:
        raise AssertionError(f"{name}: expected {expected!r}, got {actual!r}")


def assert_true(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name}: {detail}")


def test_extract_url_from_natural_request() -> None:
    service = CrawlerTemporaryExtractService()
    url = service.extract_url("总结一下 https://example.com/a/b?x=1 的内容，不用保存")
    assert_equal("url", url, "https://example.com/a/b?x=1")
    attached = service.extract_url("总结一下https://baike.baidu.com/item/%E5%95%86%E5%93%81/1245866的内容给我 不用保存到本地")
    assert_equal("attached_chinese_suffix", attached, "https://baike.baidu.com/item/%E5%95%86%E5%93%81/1245866")
    damaged = service.extract_url("总结一下https://baike.baidu.com/item/%E5%95%86%E5%93%81/1245866" + "\u003f" * 5 + " 不用保存")
    assert_equal("damaged_suffix", damaged, "https://baike.baidu.com/item/%E5%95%86%E5%93%81/1245866")


def test_html_to_text_extracts_title_and_body() -> None:
    service = CrawlerTemporaryExtractService()
    title, text = service.html_to_text("<html><head><title>商品</title></head><body><h1>商品</h1><p>商品是用于交换的劳动产品。</p></body></html>", "text/html")
    assert_equal("title", title, "商品")
    assert_true("body", "用于交换" in text)


def test_run_uses_fetch_and_does_not_save() -> None:
    service = CrawlerTemporaryExtractService()

    def fetch(url: str):
        return (
            "# 商品\n商品是用于交换的劳动产品。商品具有使用价值和价值。商品经济中，商品生产和商品交换共同构成社会经济活动的重要内容。"
            "在一般百科解释中，商品通常和市场交换、劳动产品、价值形式、货币交换等概念相关，可用于概括经济活动中的基本对象。"
            "这段测试文本用于模拟公开网页正文，确保临时抽取工具只把正文交给 CrawlerAgent 总结，而不创建本地导出目录、不触发入库流程。",
            "text/plain",
            200,
        )

    def summarize(question: str, url: str, text: str) -> str:
        assert_true("question_forwarded", "总结" in question)
        assert_true("text_forwarded", "使用价值" in text)
        return "商品是用于交换的劳动产品，具有使用价值和价值。"

    result = service.run(question="总结 https://example.com/item 不用保存", collection_target="https://example.com/item", fetch=fetch, summarize=summarize)
    response = result.to_response(agent="crawler_agent")
    assert_equal("answer", response["answer"], "商品是用于交换的劳动产品，具有使用价值和价值。")
    assert_equal("saved_flag", response["temporary_extract"]["saved_to_local"], False)
    assert_equal("source_saved_flag", response["sources"][0]["metadata"]["saved_to_local"], False)


def test_run_reviews_incomplete_answer_with_requested_terms() -> None:
    service = CrawlerTemporaryExtractService()
    calls: list[dict[str, object]] = []

    def fetch(url: str):
        return (
            "TaskGroup is an asynchronous context manager. create_task schedules a coroutine. "
            "Cancellation is used to request that a task stop at an await point. "
            "TaskGroup also cancels remaining tasks when one task fails. " * 8,
            "text/plain",
            200,
        )

    def summarize(question: str, url: str, text: str) -> str:  # noqa: ARG001
        return "基于文档，以下是关于 `"

    def review(question: str, url: str, text: str, first_answer: str, missing_terms: list[str], excerpt: str) -> str:  # noqa: ARG001
        calls.append({"missing_terms": missing_terms, "excerpt": excerpt, "first_answer": first_answer})
        return "TaskGroup 管理任务组；create_task 用于创建任务；cancellation 表示取消任务。"

    result = service.run(
        question="总结 TaskGroup、create_task、cancellation",
        collection_target="https://docs.python.org/3/library/asyncio-task.html",
        fetch=fetch,
        summarize=summarize,
        review_summarize=review,
    )
    assert_true("review_called", bool(calls))
    assert_true("missing_terms_visible", "TaskGroup" in calls[0]["missing_terms"], str(calls))
    assert_true("excerpt_has_source_terms", "create_task" in str(calls[0]["excerpt"]), str(calls[0]["excerpt"]))
    assert_true("answer_repaired", "TaskGroup" in result.answer and "create_task" in result.answer and "cancellation" in result.answer, result.answer)


def test_requested_terms_ignore_instruction_words_and_url_parts() -> None:
    service = CrawlerTemporaryExtractService()
    terms = service.requested_terms(
        "Use Crawler to temporarily read https://docs.python.org/3/library/asyncio-task.html "
        "and summarize technical points about TaskGroup, create_task, and cancellation. Do not save locally."
    )
    assert_true("keeps_taskgroup", "TaskGroup" in terms, str(terms))
    assert_true("keeps_create_task", "create_task" in terms, str(terms))
    assert_true("keeps_cancellation", "cancellation" in terms, str(terms))
    assert_true("drops_instruction_words", all(term not in terms for term in ["Use", "Crawler", "temporarily", "read", "summarize", "python", "docs"]), str(terms))


def test_complete_temporary_answer_does_not_review_instruction_words() -> None:
    service = CrawlerTemporaryExtractService()
    calls: list[str] = []

    def fetch(url: str):
        return (
            "TaskGroup manages a group of tasks. create_task schedules a coroutine. "
            "Cancellation asks a task to stop and raises CancelledError at await points. " * 8,
            "text/plain",
            200,
        )

    def summarize(question: str, url: str, text: str) -> str:  # noqa: ARG001
        return "TaskGroup manages tasks; create_task schedules coroutines; cancellation handles task stopping."

    def review(question: str, url: str, text: str, first_answer: str, missing_terms: list[str], excerpt: str) -> str:  # noqa: ARG001
        calls.append(",".join(missing_terms))
        return first_answer

    service.run(
        question=(
            "Use Crawler to temporarily read https://docs.python.org/3/library/asyncio-task.html "
            "and summarize technical points about TaskGroup, create_task, and cancellation. Do not save locally."
        ),
        collection_target="https://docs.python.org/3/library/asyncio-task.html",
        fetch=fetch,
        summarize=summarize,
        review_summarize=review,
    )
    assert_equal("review_calls", calls, [])


def test_answer_completely_mentioning_inline_code_is_not_marked_incomplete() -> None:
    service = CrawlerTemporaryExtractService()
    answer = "TaskGroup manages task groups. `create_task` schedules coroutines. Cancellation is handled by `CancelledError`."
    assert_equal("complete_answer", service.answer_looks_incomplete(answer), False)
    assert_equal("dangling_backtick", service.answer_looks_incomplete("TaskGroup summary about `create_task"), True)


if __name__ == "__main__":
    test_extract_url_from_natural_request()
    test_html_to_text_extracts_title_and_body()
    test_run_uses_fetch_and_does_not_save()
    test_run_reviews_incomplete_answer_with_requested_terms()
    test_requested_terms_ignore_instruction_words_and_url_parts()
    test_complete_temporary_answer_does_not_review_instruction_words()
    test_answer_completely_mentioning_inline_code_is_not_marked_incomplete()
    print("crawler_temporary_extract_service_scenarios passed")

