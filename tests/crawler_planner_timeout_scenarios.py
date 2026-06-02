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
    assert_equal("specific_target", _session_target_hint(summary), "乌托邦探险之旅3.0")
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
    assert_true("specific_alias_query", any("乌托邦探险之旅3.0" in query or "乌托邦探险之旅" in query for query in queries))


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


def test_rule_fallback_keeps_quoted_slash_alias_modpack_target() -> None:
    question = (
        "乌托邦探险之旅 / Utopian Journey 整合包完整公开资料：版本信息、安装要求、下载/整合包包体线索、"
        "完整模组列表、配置/manifest、更新日志、玩法路线、新手到毕业路线"
    )
    plan = plan_crawler_tasks_rule_fallback(
        question,
        ROOT / "data" / "crawler_exports",
        max_tasks=8,
        planner_error="unit timeout",
        session_summary={
            "delivery_target": "MCagent/RAG",
            "requested_by": "user_via_mcagent",
            "collection_target": question,
            "task_goal": "MCagent 转达：获取“乌托邦探险之旅 / Utopian Journey”整合包完整公开资料。",
        },
    )
    assert_equal("topic", plan["topic"], "乌托邦探险之旅 / Utopian Journey")
    queries = [task["query"] for task in plan["tasks"]]
    assert_true("no_generic_complete_public_pack", all("完整公开整合包" not in query for query in queries))
    assert_true("target_bound_queries", any("乌托邦探险之旅" in query and "模组列表" in query for query in queries))
    assert_true("english_alias_queries", any("Utopian Journey" in query for query in queries))


def test_rule_fallback_extracts_modern_utf8_chinese_modpack_handoff() -> None:
    question = (
        "请你告诉 CrawlerAgent 获取乌托邦探险之旅 Utopian Journey 整合包的完整公开资料，交给 MCagent/RAG 使用。"
        "重点包括版本、加载器、简介、下载/项目页、模组列表、任务线、玩法路线、新手到毕业攻略；"
        "让 CrawlerAgent 自己判断来源是否有效、是否需要忽略/删除/重试。"
    )
    plan = plan_crawler_tasks_rule_fallback(
        question,
        ROOT / "data" / "crawler_exports",
        max_tasks=10,
        planner_error="unit timeout",
        session_summary={
            "delivery_target": "MCagent/RAG",
            "requested_by": "user_via_mcagent",
            "collection_target": question,
            "task_goal": question + " 整合包完整公开资料；需要补齐：版本、下载/包体线索、玩法路线",
        },
    )
    assert_equal("topic", plan["topic"], "乌托邦探险之旅 / Utopian Journey")
    queries = [task["query"] for task in plan["tasks"]]
    assert_true("no_generic_complete_public_pack", all("完整公开整合包" not in query for query in queries))
    assert_true("target_cn_queries", any("乌托邦探险之旅" in query for query in queries))
    assert_true("target_en_queries", any("Utopian Journey" in query for query in queries))


def test_mcagent_delegation_extracts_modpack_entity_from_full_instruction() -> None:
    question = (
        "请先检查本地资料里乌托邦探险之旅 / Utopian Journey 整合包还缺哪些内容，然后把缺失的公开资料采集任务转交给 CrawlerAgent。"
        "目标是补齐整合包完整信息：版本、下载/包体线索、manifest/配置、完整模组列表、玩法路线、新手到毕业路线、更新日志，采集结果入库给 MCagent/RAG 使用。"
    )
    raw = {
        "topic": question,
        "package_type": "modpack",
        "delivery_target": "MCagent/RAG",
        "sources": ["mcagent_context", "modrinth", "modpack_download", "mcmod", "web_discovery"],
        "tasks": [
            {"source": "mcagent_context", "query": question, "reason": "check local gaps", "priority": 150},
            {"source": "modpack_download", "query": question, "reason": "find archive", "priority": 140},
            {"source": "modrinth", "query": "Utopian Journey", "reason": "project metadata", "priority": 130},
        ],
    }
    plan = _sanitize_plan(
        raw,
        question,
        ROOT / "data" / "crawler_exports",
        max_tasks=8,
        session_summary={
            "delivery_target": "MCagent/RAG",
            "requested_by": "user_via_mcagent",
            "collection_target": question,
            "task_goal": question,
        },
    )
    assert_equal("topic", plan["topic"], "乌托邦探险之旅 / Utopian Journey")
    queries = [task["query"] for task in plan["tasks"]]
    assert_true("no_full_instruction_query", all("先检查本地资料" not in query and "转交给 CrawlerAgent" not in query for query in queries))
    assert_true("keeps_entity_query", any(query == "乌托邦探险之旅 / Utopian Journey" or query == "Utopian Journey" for query in queries))


def test_utf8_mcagent_delegation_extracts_clean_named_alias_target() -> None:
    question = (
        "\u8bf7\u5148\u8ba9 MCagent \u68c0\u67e5\u672c\u5730\u8d44\u6599\u91cc "
        "\u4e4c\u6258\u90a6\u63a2\u9669\u4e4b\u65c5 / Utopian Journey \u6574\u5408\u5305"
        "\u8fd8\u7f3a\u54ea\u4e9b\u5185\u5bb9\uff0c\u7136\u540e\u628a\u7f3a\u5931\u7684"
        "\u516c\u5f00\u8d44\u6599\u91c7\u96c6\u4efb\u52a1\u8f6c\u4ea4\u7ed9 CrawlerAgent\u3002"
    )
    session_summary = {
        "delivery_target": "MCagent/RAG",
        "requested_by": "user_via_mcagent",
        "collection_target": "\u4e4c\u6258\u90a6\u63a2\u9669\u4e4b\u65c5 / Utopian Journey \u6574\u5408\u5305\u5b8c\u6574\u516c\u5f00\u8d44\u6599",
        "task_goal": "MCagent \u8f6c\u8fbe\uff1a\u83b7\u53d6\u4e4c\u6258\u90a6\u63a2\u9669\u4e4b\u65c5 / Utopian Journey \u6574\u5408\u5305\u5b8c\u6574\u516c\u5f00\u8d44\u6599\uff0c\u4f9b MCagent/RAG \u56de\u7b54\u3002",
    }
    raw = {
        "topic": "/ Utopian Journey ??????????? MCagent/RAG",  # encoding-check: allow
        "package_type": "modpack",
        "delivery_target": "MCagent/RAG",
        "tasks": [{"source": "mcagent_context", "query": "/ Utopian Journey ??????????? MCagent/RAG", "priority": 150}],  # encoding-check: allow
    }
    plan = _sanitize_plan(raw, question, ROOT / "data" / "crawler_exports", max_tasks=8, session_summary=session_summary)
    assert_equal("topic", plan["topic"], "\u4e4c\u6258\u90a6\u63a2\u9669\u4e4b\u65c5 / Utopian Journey")
    assert_equal("target_hint", plan["target_hint"], "\u4e4c\u6258\u90a6\u63a2\u9669\u4e4b\u65c5 / Utopian Journey")
    queries = [task["query"] for task in plan["tasks"]]
    assert_true("no_broken_query", all("?" not in query and "MCagent/RAG" not in query for query in queries))


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


def test_reflection_llm_failure_stops_before_executor_tool_choice() -> None:
    original_client = crawler_llm_planner._planner_client

    class FailingClient:
        def chat(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise RuntimeError("exceeded retry limit, last status: 429 Too Many Requests")

    crawler_llm_planner._planner_client = lambda: (FailingClient(), "fake-rate-limited")  # type: ignore[assignment]
    try:
        decision = reflect_crawler_progress(
            "collect Utopian Journey modpack complete data",
            {"topic": "Utopian Journey", "target_hint": "Utopian Journey", "delivery_target": "MCagent/RAG"},
            task_results=[{"source": "web_discovery", "query": "Utopian Journey", "returncode": 0, "empty_result": True}],
            pending_tasks=[{"source": "modpack_download", "query": "Utopian Journey", "reason": "find archive", "priority": 100}],
            session_summary={"delivery_target": "MCagent/RAG", "collection_target": "Utopian Journey modpack"},
            max_new_tasks=4,
        )
    finally:
        crawler_llm_planner._planner_client = original_client  # type: ignore[assignment]
    assert_equal("action", decision["action"], "finish")
    assert_true("no_tasks", decision["tasks"] == [])
    assert_true("reflection_issue", "reflection_llm_error" in decision["contract"]["issues"])
    assert_true("executor_boundary_issue", "stopped_before_executor_tool_choice" in decision["contract"]["issues"])
    assert_true("rate_limit_visible", "429" in decision["reason"])


def test_topic_discovery_review_uses_crawler_profile_client() -> None:
    original_client_for_agent = crawler_llm_planner.client_for_agent
    calls: list[tuple[str, float, int]] = []

    class FakeClient:
        def chat(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return "ACCEPT|web_discovery|Utopian Journey mod list|useful missing public topic"

    def fake_client_for_agent(config, agent, *, temperature=0.0, timeout_seconds=None):  # noqa: ANN001, ANN202
        calls.append((agent, temperature, timeout_seconds or 0))
        return FakeClient(), "DeepSeek crawler profile"

    crawler_llm_planner.client_for_agent = fake_client_for_agent  # type: ignore[assignment]
    try:
        plan = crawler_llm_planner.review_topic_discovery_candidates(
            "Utopian Journey",
            ["Utopian Journey mod list"],
            [],
            [],
            max_tasks=3,
        )
    finally:
        crawler_llm_planner.client_for_agent = original_client_for_agent  # type: ignore[assignment]
    assert_equal("agent", calls[0][0], "crawler_agent")
    assert_equal("planner_model", plan["planner_model"], "DeepSeek crawler profile")
    assert_equal("source", plan["tasks"][0]["source"], "web_discovery")


def test_fallback_plan_confirmation_lets_crawler_pick_existing_task() -> None:
    original_client = crawler_llm_planner._planner_client

    class FakeClient:
        def chat(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return '{"action":"execute_pending","selected_index":1,"reason":"download discovery should run before internal parsing","tasks":[]}'

    crawler_llm_planner._planner_client = lambda: (FakeClient(), "fake-confirm")  # type: ignore[assignment]
    try:
        decision = reflect_crawler_progress(
            "collect Utopian Journey modpack complete data",
            {"topic": "Utopian Journey", "target_hint": "Utopian Journey", "delivery_target": "MCagent/RAG", "strategy": "target_fallback_after_llm_planner_error"},
            task_results=[],
            pending_tasks=[
                {"source": "modpack_internal", "query": "Utopian Journey", "reason": "parse archive", "priority": 145},
                {"source": "modpack_download", "query": "Utopian Journey", "reason": "find public archive", "priority": 130},
            ],
            session_summary={"delivery_target": "MCagent/RAG", "collection_target": "Utopian Journey modpack"},
            max_new_tasks=4,
        )
    finally:
        crawler_llm_planner._planner_client = original_client  # type: ignore[assignment]
    assert_equal("action", decision["action"], "execute_pending")
    assert_equal("selected_index", decision["selected_index"], 1)
    assert_true("planner_label", "fallback-confirmation" in decision["planner"])


def test_fallback_plan_confirmation_failure_stops_before_tools() -> None:
    original_client = crawler_llm_planner._planner_client

    class FailingClient:
        def chat(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise TimeoutError("timed out")

    crawler_llm_planner._planner_client = lambda: (FailingClient(), "fake-confirm")  # type: ignore[assignment]
    try:
        decision = reflect_crawler_progress(
            "collect Utopian Journey modpack complete data",
            {"topic": "Utopian Journey", "target_hint": "Utopian Journey", "delivery_target": "MCagent/RAG", "strategy": "target_fallback_after_llm_planner_error"},
            task_results=[],
            pending_tasks=[{"source": "modpack_download", "query": "Utopian Journey", "reason": "find public archive", "priority": 130}],
            session_summary={"delivery_target": "MCagent/RAG", "collection_target": "Utopian Journey modpack"},
            max_new_tasks=4,
        )
    finally:
        crawler_llm_planner._planner_client = original_client  # type: ignore[assignment]
    assert_equal("action", decision["action"], "finish")
    assert_true("confirmation_issue", "fallback_confirmation_llm_error" in decision["contract"]["issues"])
    assert_true("executor_boundary_issue", "stopped_before_executor_tool_choice" in decision["contract"]["issues"])


def test_fallback_plan_confirmation_empty_json_reports_real_failure() -> None:
    original_client = crawler_llm_planner._planner_client

    class EmptyClient:
        def chat(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return ""

    crawler_llm_planner._planner_client = lambda: (EmptyClient(), "fake-confirm")  # type: ignore[assignment]
    try:
        decision = reflect_crawler_progress(
            "collect Utopian Journey modpack complete data",
            {"topic": "Utopian Journey", "target_hint": "Utopian Journey", "delivery_target": "MCagent/RAG", "strategy": "target_fallback_after_llm_planner_error"},
            task_results=[],
            pending_tasks=[{"source": "modpack_download", "query": "Utopian Journey", "reason": "find public archive", "priority": 130}],
            session_summary={"delivery_target": "MCagent/RAG", "collection_target": "Utopian Journey modpack"},
            max_new_tasks=4,
        )
    finally:
        crawler_llm_planner._planner_client = original_client  # type: ignore[assignment]
    assert_equal("action", decision["action"], "finish")
    assert_true("confirmation_issue", "fallback_confirmation_llm_error" in decision["contract"]["issues"])
    assert_true("real_error_visible", "fallback confirmation returned empty JSON" in decision["reason"])
    assert_true("no_broken_output_leak", "No broken output provided" not in decision["done_summary"])


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


def test_modpack_archive_fallback_does_not_become_browser_collect() -> None:
    plan = plan_crawler_tasks_rule_fallback(
        "Collect complete public data for Minecraft modpack Utopian Journey. Find and fully automatically download a public .mrpack or .zip archive. Start with Modrinth files.url, CurseForge downloadUrl, GitHub Releases, packwiz, then forum direct links.",
        ROOT / "data" / "crawler_exports",
        max_tasks=6,
        planner_error="unit timeout",
        session_summary={
            "delivery_target": "MCagent/RAG",
            "requested_by": "mcagent",
            "collection_target": "Collect complete public data for Minecraft modpack Utopian Journey and download a public .mrpack or .zip archive.",
        },
    )
    sources = [task["source"] for task in plan["tasks"]]
    assert_equal("strategy", plan["strategy"], "target_fallback_after_llm_planner_error")
    assert_equal("first_source", sources[0], "modpack_download")
    assert_true("has_archive_route", "modpack_download" in sources and "modrinth" in sources)
    assert_true("not_structured_browser", all(source != "browser_collect" for source in sources))


def test_english_modpack_archive_fallback_extracts_pack_name_and_download_query() -> None:
    question = (
        'Collect complete public data for the Minecraft modpack "Utopian Journey" (Chinese name: 乌托邦探险之旅). '
        "The primary objective is to locate and fully automatically download a public .mrpack or .zip modpack archive. "
        "Start with public archive routes: Modrinth modpack versions files.url, CurseForge public/API file pages with direct downloadUrl, GitHub Releases assets, packwiz pack.toml/index.toml repositories, then forum/community direct links."
    )
    plan = plan_crawler_tasks_rule_fallback(
        question,
        ROOT / "data" / "crawler_exports",
        max_tasks=6,
        planner_error="unit timeout",
        session_summary={"delivery_target": "MCagent/RAG", "requested_by": "mcagent", "collection_target": question},
    )
    assert_equal("topic", plan["topic"], "Utopian Journey")
    first = plan["tasks"][0]
    assert_equal("first_source", first["source"], "modpack_download")
    assert_true("download_query_has_archive_terms", "Utopian Journey" in first["query"] and ".mrpack" in first["query"])


def test_english_handoff_extracts_pack_name_from_for_the_minecraft_modpack_phrase() -> None:
    question = (
        "Collect complete public data for the Craftoria Minecraft modpack. "
        "Prioritize finding a public fully automatic .mrpack or .zip archive download, download it, "
        "parse internal files, self-audit accepted and rejected sources with ingest status, then deliver evidence to MCagent/RAG for answering."
    )
    plan = plan_crawler_tasks_rule_fallback(
        question,
        ROOT / "data" / "crawler_exports",
        max_tasks=6,
        planner_error="unit timeout",
        session_summary={"delivery_target": "MCagent/RAG", "requested_by": "mcagent", "collection_target": question},
    )
    assert_equal("topic", plan["topic"], "Craftoria")
    first = plan["tasks"][0]
    assert_equal("first_source", first["source"], "modpack_download")
    assert_true(f"short_download_query: {first['query']}", first["query"].startswith("Craftoria "))
    assert_true(f"archive_terms: {first['query']}", "modpack" in first["query"] and ".mrpack" in first["query"] and ".zip" in first["query"])
    assert_true(f"no_instruction_leak: {first['query']}", "complete public data" not in first["query"].lower())


def test_type_discovery_request_does_not_force_modpack_archive_download() -> None:
    question = (
        "\u8bf7\u4f60\u4f5c\u4e3a MCagent \u8f6c\u8fbe CrawlerAgent\uff1a"
        "\u4ee5\u519c\u592b\u4e50\u4e8b / Farmer's Delight \u4e3a\u4f8b\u5b50\u8fdb\u884c\u6293\u53d6\u6d4b\u8bd5\uff0c"
        "Crawler \u81ea\u5df1\u5224\u65ad\u5b83\u662f\u6a21\u7ec4\u8fd8\u662f\u6574\u5408\u5305\uff1b"
        "\u5982\u679c\u4e0d\u662f\u6574\u5408\u5305\uff0c\u4e0d\u8981\u5f3a\u884c\u8dd1\u5305\u4f53\u4e0b\u8f7d\u3002"
    )
    plan = plan_crawler_tasks_rule_fallback(
        question,
        ROOT / "data" / "crawler_exports",
        max_tasks=8,
        planner_error="unit timeout",
        session_summary={
            "delivery_target": "MCagent/RAG",
            "requested_by": "user_via_mcagent",
            "collection_target": "\u519c\u592b\u4e50\u4e8b / Farmer's Delight \u516c\u5f00\u8d44\u6599\u91c7\u96c6\uff1bCrawlerAgent \u81ea\u884c\u5224\u65ad\u76ee\u6807\u7c7b\u578b\uff1b\u5982\u679c\u4e0d\u662f\u6574\u5408\u5305\uff0c\u4e0d\u5f3a\u5236\u5305\u4f53\u4e0b\u8f7d",
            "task_goal": question,
        },
    )
    assert_equal("topic", plan["topic"], "\u519c\u592b\u4e50\u4e8b / Farmer's Delight")
    sources = [task["source"] for task in plan["tasks"]]
    assert_true("does_not_start_with_modpack_download", not sources or sources[0] != "modpack_download")
    assert_true("keeps_public_discovery", any(source in {"modrinth", "mcmod", "web_discovery", "playwright", "topic_discovery"} for source in sources))


def test_create_mod_fallback_does_not_inject_unrelated_component_queries() -> None:
    question = (
        "Collect public information for the Minecraft mod 'Create' (Chinese alias: 机械动力). "
        "Determine it is a mod, not a modpack, and avoid forced archive download. "
        "Prioritize project overview, supported loaders/versions, official docs or wiki, "
        "core mechanics, beginner automation, rotational power, stress, and logistics guide material."
    )
    plan = plan_crawler_tasks_rule_fallback(
        question,
        ROOT / "data" / "crawler_exports",
        max_tasks=12,
        planner_error="unit timeout",
        session_summary={
            "delivery_target": "MCagent/RAG",
            "requested_by": "user_via_mcagent",
            "collection_target": question,
            "task_goal": question,
        },
    )
    queries = " ".join(str(task.get("query") or "") for task in plan["tasks"])
    assert_true("target_kept", "Create" in queries)
    assert_true("no_tacz_pollution", "TACZ" not in queries and "Timeless and Classics" not in queries)
    assert_true("no_archive_first", not plan["tasks"] or plan["tasks"][0]["source"] != "modpack_download")


def test_reflection_allows_url_seen_in_manifest_preview() -> None:
    original_client = crawler_llm_planner._planner_client

    class FakeClient:
        def chat(self, messages, *, temperature, max_tokens):  # noqa: ANN001, ARG002
            return (
                '{"action":"add_tasks","reason":"inspect discovered page",'
                '"tasks":[{"source":"playwright","query":"https://bbsmc.net/modpack/utopia-journey",'
                '"reason":"inspect BBSMC project page","priority":110}]}'
            )

    crawler_llm_planner._planner_client = lambda: (FakeClient(), "fake")  # type: ignore[assignment]
    try:
        decision = reflect_crawler_progress(
            "Collect Utopian Journey modpack archive",
            {"topic": "Utopian Journey", "target_hint": "Utopian Journey", "delivery_target": "MCagent/RAG"},
            [
                {
                    "source": "modpack_download",
                    "returncode": 0,
                    "manifest_stats": {"records": 1, "skipped": 1, "errors": 0, "downloads": 0, "candidates": 0, "blockers": 2},
                    "observation": {"status": "empty", "summary": "no direct archive", "retryable": True},
                    "failure_reason": "Observed cloud-drive/client-only blocker(s).",
                    "manifest_preview": {
                        "search_results": [{"title": "乌托邦探险之旅", "url": "https://bbsmc.net/modpack/utopia-journey"}],
                        "blockers": [{"project_url": "https://bbsmc.net/modpack/utopia-journey", "url": "https://pan.quark.cn/s/76148f08445c"}],
                    },
                }
            ],
            [],
            session_summary={"delivery_target": "MCagent/RAG"},
        )
    finally:
        crawler_llm_planner._planner_client = original_client  # type: ignore[assignment]
    assert_equal("action", decision["action"], "add_tasks")
    assert_equal("query", decision["tasks"][0]["query"], "https://bbsmc.net/modpack/utopia-journey")
    assert_equal("grounded", decision["tasks"][0]["from_discovered_candidate"], True)


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
    test_rule_fallback_keeps_quoted_slash_alias_modpack_target()
    test_rule_fallback_extracts_modern_utf8_chinese_modpack_handoff()
    test_mcagent_delegation_extracts_modpack_entity_from_full_instruction()
    test_utf8_mcagent_delegation_extracts_clean_named_alias_target()
    test_session_target_rejects_generic_relation_phrase()
    test_gap_collection_fallback_prefers_generic_web_and_bound_queries()
    test_llm_plan_gap_collection_is_rebalanced_to_generic_tools()
    test_gap_collection_rejects_literal_missing_as_web_topic()
    test_reflection_replaces_literal_gap_pending_query()
    test_reflection_llm_failure_stops_before_executor_tool_choice()
    test_topic_discovery_review_uses_crawler_profile_client()
    test_fallback_plan_confirmation_lets_crawler_pick_existing_task()
    test_fallback_plan_confirmation_failure_stops_before_tools()
    test_fallback_plan_confirmation_empty_json_reports_real_failure()
    test_structured_xlsx_request_uses_browser_collect()
    test_modpack_archive_fallback_does_not_become_browser_collect()
    test_english_modpack_archive_fallback_extracts_pack_name_and_download_query()
    test_english_handoff_extracts_pack_name_from_for_the_minecraft_modpack_phrase()
    test_type_discovery_request_does_not_force_modpack_archive_download()
    test_create_mod_fallback_does_not_inject_unrelated_component_queries()
    test_reflection_allows_url_seen_in_manifest_preview()
    test_job_planner_timeout_returns_executable_fallback()
    print("crawler_planner_timeout_scenarios passed")
