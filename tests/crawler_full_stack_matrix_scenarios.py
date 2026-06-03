from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.crawler_capabilities import task_preflight  # noqa: E402
from mcagent.crawler_llm_planner import plan_crawler_tasks_rule_fallback  # noqa: E402


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def sources(plan: dict) -> list[str]:
    return [str(task.get("source") or "") for task in plan.get("tasks") or []]


def queries(plan: dict) -> list[str]:
    return [str(task.get("query") or "") for task in plan.get("tasks") or []]


def test_general_document_research_uses_web_browser_fetch_stack() -> None:
    question = "Collect Python requests official documentation, GitHub repository, releases, and usage examples for a general RAG corpus."
    plan = plan_crawler_tasks_rule_fallback(
        question,
        ROOT / "data" / "crawler_exports",
        max_tasks=8,
        planner_error="matrix",
        session_summary={"delivery_target": "RAG", "requested_by": "user", "task_goal": question},
    )
    plan_sources = sources(plan)
    assert_true("has_web_discovery", "web_discovery" in plan_sources)
    assert_true("has_browser", "playwright" in plan_sources)
    assert_true("no_minecraft", all(source not in {"mcmod", "modrinth", "modpack_download", "modpack_internal"} for source in plan_sources))
    assert_true("target_query", any("Python requests" in query for query in queries(plan)))


def test_structured_browser_task_keeps_browser_collect_constraints() -> None:
    question = (
        "用 Crawler 打开 https://webscraper.io/test-sites/e-commerce/static/computers/laptops "
        "提取前 5 个商品的名称、价格、链接，保存为 xlsx、csv、json 到 D:\\magic\\MC_Agent\\data\\manual_tests\\matrix_items。"
    )
    plan = plan_crawler_tasks_rule_fallback(
        question,
        ROOT / "data" / "crawler_exports",
        max_tasks=6,
        planner_error="matrix",
        session_summary={
            "delivery_target": "human",
            "requested_by": "user",
            "task_goal": question,
            "output_dir": r"D:\magic\MC_Agent\data\manual_tests\matrix_items",
            "max_items": 5,
            "fields": ["name", "price", "url"],
        },
    )
    browser_tasks = [task for task in plan.get("tasks") or [] if task.get("source") == "browser_collect"]
    assert_true("browser_collect", bool(browser_tasks))
    assert_true("fields", browser_tasks[0].get("fields") == ["name", "price", "url"])
    assert_true("output_dir", str(browser_tasks[0].get("output_dir") or "").endswith("matrix_items"))


def test_local_file_task_uses_local_group_not_web_search() -> None:
    question = r"Search local files under D:\magic\MC_Agent\docs for crawler capability registry and save snippets."
    plan = plan_crawler_tasks_rule_fallback(
        question,
        ROOT / "data" / "crawler_exports",
        max_tasks=6,
        planner_error="matrix",
        session_summary={
            "delivery_target": "human",
            "requested_by": "user",
            "task_goal": question,
            "path": r"D:\magic\MC_Agent\docs",
        },
    )
    plan_sources = sources(plan)
    assert_true("local_search_available", "search_local_files" in plan_sources or "read_local_file" in plan_sources)
    assert_true("no_minecraft", all(source not in {"mcmod", "modrinth", "modpack_download"} for source in plan_sources))


def test_minecraft_modpack_task_loads_domain_plugin_and_archive_route() -> None:
    question = "获取乌托邦探险之旅 / Utopian Journey Minecraft modpack 的完整资料和可全自动下载包体路线。"
    plan = plan_crawler_tasks_rule_fallback(
        question,
        ROOT / "data" / "crawler_exports",
        max_tasks=8,
        planner_error="matrix",
        session_summary={"delivery_target": "MCagent/RAG", "requested_by": "user_via_mcagent", "task_goal": question},
    )
    plan_sources = sources(plan)
    assert_true("archive_route", "modpack_download" in plan_sources)
    assert_true("minecraft_domain", any(source in {"mcmod", "modrinth", "followup"} for source in plan_sources))
    assert_true("general_still_available", any(source in {"web_discovery", "playwright"} for source in plan_sources))


def test_preflight_blocks_tool_contract_without_deciding_relevance() -> None:
    invalid = task_preflight({"source": "save_artifact", "query": "save summary"})
    assert_true("invalid", not invalid["valid"])
    assert_true("requires_content", any(str(issue).startswith("requires_any:") for issue in invalid["issues"]))
    assert_true("objective_contract", "CrawlerAgent decides relevance" in invalid["objective_contract"])
    valid = task_preflight({"source": "save_artifact", "query": "save summary", "content": "already reviewed by CrawlerAgent"})
    assert_true("valid", valid["valid"])


if __name__ == "__main__":
    test_general_document_research_uses_web_browser_fetch_stack()
    test_structured_browser_task_keeps_browser_collect_constraints()
    test_local_file_task_uses_local_group_not_web_search()
    test_minecraft_modpack_task_loads_domain_plugin_and_archive_route()
    test_preflight_blocks_tool_contract_without_deciding_relevance()
    print("crawler_full_stack_matrix_scenarios passed")
