from __future__ import annotations

from pathlib import Path
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.config import load_config  # noqa: E402
from mcagent.crawler_llm_planner import _sanitize_plan, _session_target_hint, plan_crawler_tasks_rule_fallback, reflect_crawler_progress  # noqa: E402
import mcagent.crawler_llm_planner as crawler_llm_planner  # noqa: E402
from mcagent.web_server import Job, _plan_crawler_with_job_timeout  # noqa: E402
import mcagent.web_server as web_server  # noqa: E402


def assert_equal(name: str, actual, expected) -> None:
    if actual != expected:
        raise AssertionError(f"{name}: expected {expected!r}, got {actual!r}")


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def utopia_question() -> str:
    return "问下MCAgent乌托邦整合包还缺哪些东西 你去网上找补给他"


def utopia_session_summary() -> dict[str, str]:
    question = utopia_question()
    goal = f"根据 MCagent/RAG 本地上下文与缺口，为该主题采集缺失资料并交付给 MCagent/RAG。 用户原始目标：{question}"
    return {
        "delivery_target": "MCagent/RAG",
        "requested_by": "user",
        "collection_target": goal,
        "task_goal": goal,
    }


def test_direct_crawler_delegate_phrase_is_not_target() -> None:
    summary = {
        "delivery_target": "MCagent/RAG",
        "requested_by": "mcagent",
        "collection_target": "叫Crawler帮你采集乌托邦缺失的资料",
        "task_goal": "针对乌托邦探险之旅3.0整合包采集缺失资料并交付给 MCagent/RAG。",
        "mcagent_gap_summary": "本地资料确认主题是乌托邦探险之旅3.0整合包，缺少模组清单、任务线、Boss 攻略和新手路线。",
    }
    assert_equal("specific_target", _session_target_hint(summary), "乌托邦探险之旅3.0整合包")
    plan = plan_crawler_tasks_rule_fallback(
        "叫Crawler帮你采集乌托邦缺失的资料",
        ROOT / "data" / "crawler_exports",
        max_tasks=8,
        planner_error="unit timeout",
        session_summary=summary,
    )
    queries = [task["query"] for task in plan["tasks"]]
    assert_true("no_delegate_phrase_query", all("叫Crawler" not in query and "缺失" not in query for query in queries))
    assert_true("no_stale_specific_pack", all("落幕曲" not in query for query in queries))
    assert_true("specific_alias_query", any("乌托邦探险之旅3.0整合包" in query or "乌托邦探险之旅" in query for query in queries))


def test_rule_fallback_extracts_domain_target_from_agent_handoff() -> None:
    plan = plan_crawler_tasks_rule_fallback(
        utopia_question(),
        ROOT / "data" / "crawler_exports",
        max_tasks=8,
        planner_error="unit timeout",
        session_summary=utopia_session_summary(),
    )
    assert_equal("topic", plan["topic"], "乌托邦整合包")
    assert_equal("delivery", plan["delivery_target"], "MCagent/RAG")
    queries = [task["query"] for task in plan["tasks"]]
    assert_true("has_tasks", len(queries) > 0)
    assert_true("no_agent_query", all("MCagent/RAG" not in query and "用户原始目标" not in query for query in queries))
    assert_true("no_duplicate_pack_suffix", all("整合包 整合包" not in query for query in queries))
    assert_true("includes_known_alias", any("乌托邦探险之旅" in query or "Utopian Journey" in query for query in queries))


def test_session_target_rejects_generic_relation_phrase() -> None:
    summary = {
        "collection_target": "的相关整合包",
        "task_goal": "问下MCAgent乌托邦整合包还缺哪些东西，你去网上找补给他",
    }
    assert_equal("target", _session_target_hint(summary), "乌托邦整合包")


def test_gap_collection_fallback_prefers_generic_web_and_bound_queries() -> None:
    plan = plan_crawler_tasks_rule_fallback(
        utopia_question(),
        ROOT / "data" / "crawler_exports",
        max_tasks=10,
        planner_error="unit timeout",
        session_summary=utopia_session_summary()
        | {
            "mcagent_gap_summary": "本地资料缺口：新手路线、FTB任务、任务系统、玩法攻略、Boss 信息都不足，需要去网上找补给 MCagent/RAG。",
            "gaps": ["FTB任务", "任务系统", "新手攻略"],
        },
    )
    tasks = plan["tasks"]
    assert_true("has_tasks", len(tasks) >= 4)
    assert_equal("asks_mcagent_first", tasks[0]["source"], "mcagent_context")
    first_sources = [task["source"] for task in tasks[:4]]
    assert_true("generic_first", any(source in first_sources for source in ("mcagent_context", "web_discovery", "playwright", "modpack_download")))
    assert_true("not_mc_only", any(task["source"] in {"web_discovery", "playwright"} for task in tasks))
    queries = [task["query"] for task in tasks]
    assert_true("bound_ftb", "FTB任务" not in queries and any("乌托邦整合包 FTB任务" in query for query in queries))
    assert_true("no_non_url_fetch", all(task["source"] != "fetch_url" or task["query"].startswith(("http://", "https://")) for task in tasks))


def test_llm_plan_gap_collection_is_rebalanced_to_generic_tools() -> None:
    raw = {
        "topic": "乌托邦整合包",
        "delivery_target": "MCagent/RAG",
        "sources": ["mcmod", "web_discovery", "playwright", "modpack_download"],
        "subqueries": ["乌托邦探险之旅", "的相关整合包 Boss"],
        "tasks": [
            {"source": "mcmod", "query": "乌托邦探险之旅", "reason": "project page", "priority": 100},
            {"source": "web_discovery", "query": "模组列表", "reason": "public web", "priority": 80},
            {"source": "playwright", "query": "https://www.curseforge.com/minecraft/modpacks/utopian-journey", "reason": "render", "priority": 95},
        ],
    }
    plan = _sanitize_plan(
        raw,
        utopia_question(),
        ROOT / "data" / "crawler_exports",
        max_tasks=8,
        session_summary=utopia_session_summary()
        | {
            "mcagent_gap_summary": "本地资料缺口：模组列表、任务系统、Boss 信息，需要去网上找补给 MCagent/RAG。",
        },
    )
    tasks = plan["tasks"]
    assert_equal("mcagent_context_inserted", tasks[0]["source"], "mcagent_context")
    assert_true("generic_first", any(task["source"] in {"web_discovery", "playwright", "modpack_download"} for task in tasks[:4]))
    assert_true("mcmod_not_first", tasks[0]["source"] != "mcmod")
    assert_true("bad_query_removed", all(not str(task["query"]).startswith("的相关") for task in tasks))
    assert_true("helper_query_bound", any(task["query"] == "乌托邦整合包 模组列表" for task in tasks))


def test_ungrounded_exact_url_is_discovered_before_fetch() -> None:
    raw = {
        "topic": "Utopian Journey",
        "delivery_target": "MCagent/RAG",
        "package_type": "modpack",
        "sources": ["playwright", "modpack_download"],
        "tasks": [
            {
                "source": "playwright",
                "query": "https://github.com/Utopia-Exploration/Modpack",
                "reason": "guessed repository URL",
                "priority": 120,
            }
        ],
    }
    plan = _sanitize_plan(
        raw,
        "collect Utopian Journey modpack complete data for MCagent/RAG",
        ROOT / "data" / "crawler_exports",
        max_tasks=6,
        session_summary={"delivery_target": "MCagent/RAG", "collection_target": "Utopian Journey modpack"},
    )
    tasks = plan["tasks"]
    guessed = [task for task in tasks if task.get("original_unverified_url")]
    assert_true("downgraded_guess", bool(guessed))
    assert_equal("source", guessed[0]["source"], "web_discovery")
    assert_true("keeps_modpack_download", any(task["source"] == "modpack_download" for task in tasks))


def test_reflection_downgrades_ungrounded_exact_url() -> None:
    original_client = crawler_llm_planner._planner_client

    class FakeClient:
        def chat(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return '{"action":"add_tasks","reason":"try guessed repo","tasks":[{"source":"playwright","query":"https://github.com/Utopia-Exploration/Modpack","reason":"guessed repo","priority":120}]}'

    crawler_llm_planner._planner_client = lambda: (FakeClient(), "fake-reflection")  # type: ignore[assignment]
    try:
        decision = reflect_crawler_progress(
            "collect Utopian Journey modpack complete data",
            {"topic": "Utopian Journey", "target_hint": "Utopian Journey", "delivery_target": "MCagent/RAG"},
            task_results=[{"source": "web_discovery", "query": "Utopian Journey", "returncode": 0, "empty_result": True}],
            pending_tasks=[],
            session_summary={"delivery_target": "MCagent/RAG", "collection_target": "Utopian Journey modpack"},
            max_new_tasks=4,
        )
    finally:
        crawler_llm_planner._planner_client = original_client  # type: ignore[assignment]
    assert_equal("action", decision["action"], "add_tasks")
    assert_equal("source", decision["tasks"][0]["source"], "web_discovery")
    assert_true("unverified_saved", decision["tasks"][0].get("original_unverified_url") == "https://github.com/Utopia-Exploration/Modpack")


def test_gap_collection_rejects_literal_missing_as_web_topic() -> None:
    raw = {
        "topic": "乌托邦整合包",
        "delivery_target": "MCagent/RAG",
        "sources": ["web_discovery", "playwright", "mcmod"],
        "subqueries": ["乌托邦探险之旅 缺少 模组", "乌托邦探险之旅 待添加", "乌托邦探险之旅 还缺什么"],
        "tasks": [
            {"source": "web_discovery", "query": "乌托邦探险之旅 缺少 模组", "reason": "wrong literal gap query", "priority": 100},
            {"source": "web_discovery", "query": "乌托邦探险之旅 待添加", "reason": "wrong roadmap query", "priority": 99},
            {"source": "playwright", "query": "乌托邦探险之旅 还缺什么", "reason": "wrong meta query", "priority": 98},
        ],
    }
    plan = _sanitize_plan(
        raw,
        utopia_question(),
        ROOT / "data" / "crawler_exports",
        max_tasks=24,
        session_summary=utopia_session_summary()
        | {
            "mcagent_gap_summary": "本地资料缺口：完整模组列表、任务/阶段攻略、Boss 信息、新手路线、版本差异与更新日志。",
            "gaps": ["完整模组列表", "任务线", "Boss 信息", "新手路线", "版本差异与更新日志"],
        },
    )
    queries = [task["query"] for task in plan["tasks"]]
    assert_true("no_literal_missing_query", all("缺少" not in query and "待添加" not in query and "还缺什么" not in query for query in queries))
    assert_true("positive_modlist_query", any("模组列表" in query for query in queries))
    assert_true("positive_quest_query", any("任务" in query or "FTB" in query for query in queries))
    assert_true("positive_changelog_query", any("更新日志" in query or "版本差异" in query or "changelog" in query.lower() for query in queries))
    assert_true("no_agent_handoff_query", all("MCagent/RAG" not in query and "用户原始目标" not in query for query in queries))


def test_reflection_replaces_literal_gap_pending_query() -> None:
    original_client = crawler_llm_planner._planner_client

    class FakeClient:
        def chat(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return '{"action":"execute_pending","selected_index":0,"reason":"try literal missing query","tasks":[]}'

    crawler_llm_planner._planner_client = lambda: (FakeClient(), "fake-reflection")  # type: ignore[assignment]
    try:
        decision = reflect_crawler_progress(
            utopia_question(),
            {
                "topic": "乌托邦整合包",
                "target_hint": "乌托邦整合包",
                "delivery_target": "MCagent/RAG",
                "coverage_goals": ["完整模组列表", "任务线", "Boss 信息"],
            },
            task_results=[
                {"source": "web_discovery", "query": "乌托邦探险之旅 缺少 模组", "empty_result": True, "returncode": 0}
            ],
            pending_tasks=[
                {"source": "web_discovery", "query": "乌托邦探险之旅 缺少 模组", "reason": "bad literal gap query", "priority": 100}
            ],
            session_summary=utopia_session_summary()
            | {
                "mcagent_gap_summary": "本地资料缺口：完整模组列表、任务线、Boss 信息、新手路线。",
                "gaps": ["完整模组列表", "任务线", "Boss 信息", "新手路线"],
            },
            max_new_tasks=4,
        )
    finally:
        crawler_llm_planner._planner_client = original_client  # type: ignore[assignment]
    assert_equal("action", decision["action"], "add_tasks")
    queries = [task["query"] for task in decision["tasks"]]
    assert_true("replacement_tasks", len(queries) > 0)
    assert_true("no_literal_missing_replacement", all("缺少" not in query and "待添加" not in query for query in queries))
    assert_true("positive_replacement", any("模组列表" in query or "任务" in query or "Boss" in query for query in queries))


def test_structured_xlsx_request_uses_browser_collect() -> None:
    plan = plan_crawler_tasks_rule_fallback(
        "从公开商品网站采集 20 个商品名称、价格、链接，保存为 xlsx 到 C:\\Temp\\crawler-products",
        ROOT / "data" / "crawler_exports",
        max_tasks=5,
        planner_error="unit timeout",
        session_summary={
            "delivery_target": "human",
            "requested_by": "user",
            "collection_target": "从公开商品网站采集 20 个商品名称、价格、链接，保存为 xlsx 到 C:\\Temp\\crawler-products",
            "output_dir": "C:\\Temp\\crawler-products",
            "fields": ["name", "price", "url"],
            "max_items": 20,
        },
    )
    assert_equal("strategy", plan["strategy"], "structured_browser_fallback_after_llm_planner_error")
    assert_equal("source", plan["tasks"][0]["source"], "browser_collect")
    assert_equal("output_dir", plan["tasks"][0]["output_dir"], "C:\\Temp\\crawler-products")
    assert_equal("max_items", plan["tasks"][0]["max_items"], 20)
    assert_true("xlsx_policy", "XLSX" in plan["cleaning_policy"])


def test_job_planner_timeout_returns_executable_fallback() -> None:
    original = web_server.plan_crawler_tasks_resilient

    def slow_planner(*args, **kwargs):  # noqa: ANN002, ANN003
        time.sleep(2)
        return {"tasks": []}

    web_server.plan_crawler_tasks_resilient = slow_planner  # type: ignore[assignment]
    try:
        job = Job(id="unit", kind="crawler", title="unit")
        plan = _plan_crawler_with_job_timeout(
            job,
            utopia_question(),
            load_config(),
            max_tasks=6,
            session_summary=utopia_session_summary(),
            timeout_seconds=1,
        )
    finally:
        web_server.plan_crawler_tasks_resilient = original  # type: ignore[assignment]
    assert_equal("timeout", plan["planner_timeout_seconds"], 1)
    assert_equal("topic", plan["topic"], "乌托邦整合包")
    assert_true("fallback_tasks", len(plan["tasks"]) > 0)


if __name__ == "__main__":
    test_direct_crawler_delegate_phrase_is_not_target()
    test_rule_fallback_extracts_domain_target_from_agent_handoff()
    test_session_target_rejects_generic_relation_phrase()
    test_gap_collection_fallback_prefers_generic_web_and_bound_queries()
    test_llm_plan_gap_collection_is_rebalanced_to_generic_tools()
    test_gap_collection_rejects_literal_missing_as_web_topic()
    test_reflection_replaces_literal_gap_pending_query()
    test_structured_xlsx_request_uses_browser_collect()
    test_job_planner_timeout_returns_executable_fallback()
    print("crawler_planner_timeout_scenarios passed")
