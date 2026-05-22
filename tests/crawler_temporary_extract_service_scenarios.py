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


def test_reader_url_wraps_plain_url() -> None:
    service = CrawlerTemporaryExtractService()
    assert_equal("reader", service.reader_url("https://example.com/a"), "https://r.jina.ai/http://example.com/a")
    assert_equal("already_reader", service.reader_url("https://r.jina.ai/http://example.com/a"), "https://r.jina.ai/http://example.com/a")


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


if __name__ == "__main__":
    test_extract_url_from_natural_request()
    test_reader_url_wraps_plain_url()
    test_html_to_text_extracts_title_and_body()
    test_run_uses_fetch_and_does_not_save()
    print("crawler_temporary_extract_service_scenarios passed")
