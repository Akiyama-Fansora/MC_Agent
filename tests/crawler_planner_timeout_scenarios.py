from __future__ import annotations

from pathlib import Path
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.config import load_config  # noqa: E402
from mcagent.crawler_llm_planner import _collection_target_hint, _sanitize_plan, _session_target_hint, plan_crawler_tasks_rule_fallback, reflect_crawler_progress  # noqa: E402
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


def test_rule_fallback_rejects_numbered_action_plan_fragments_as_targets() -> None:
    question = utopia_question()
    plan = plan_crawler_tasks_rule_fallback(
        question,
        ROOT / "data" / "crawler_exports",
        max_tasks=8,
        planner_error="planner exceeded 90s startup timeout",
        session_summary={
            "delivery_target": "MCagent/RAG",
            "requested_by": "user",
            "collection_target": "1)整合包\n2)模组列表\n3)玩法路线",
            "task_goal": "1)整合包\n2)下载资料\n3)公开网页",
            "handoff_brief": f"用户原始目标：{question}\n行动计划：\n1)整合包\n2)公开资料\n3)入库",
            "planning_instruction": "CrawlerAgent should run mcagent_context as the first internal task, then collect public web data.",
        },
    )
    assert_true(f"target_is_real_entity: {plan['topic']}", "乌托邦" in plan["topic"])
    queries = [str(task.get("query") or "") for task in plan["tasks"]]
    assert_true("no_numbered_fragment_query", all(not query.lstrip().startswith(("1)", "1.", "步骤1")) for query in queries))
    assert_true(f"queries_bound_to_real_target: {queries}", any("乌托邦" in query for query in queries))


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


def test_session_target_rejects_local_knowledge_prefix_and_keeps_subject() -> None:
    summary = {
        "delivery_target": "MCagent/RAG",
        "requested_by": "user_via_mcagent",
        "collection_target": "补充本地知识库中缺失的关于乌托邦整合包的资料，包括整合包简介、模组列表、特色玩法、常见问题等。",
        "task_goal": "补充本地知识库中缺失的关于乌托邦整合包的资料，包括整合包简介、模组列表、特色玩法、常见问题等。",
        "mcagent_gap_summary": "本地已有 BBSMC 下载页和 MCBBS 搬运帖，缺少乌托邦探险之旅完整模组列表和玩法路线。",
    }
    assert_equal("target", _session_target_hint(summary), "乌托邦整合包")
    plan = plan_crawler_tasks_rule_fallback(
        "补充本地知识库中缺失的关于乌托邦整合包的资料，包括整合包简介、模组列表、特色玩法、常见问题等。",
        ROOT / "data" / "crawler_exports",
        max_tasks=8,
        planner_error="planner exceeded 90s startup timeout",
        session_summary=summary,
    )
    assert_equal("topic", plan["topic"], "乌托邦整合包")
    assert_true("no_generic_local_query", all("本地知识库中" not in task["query"] for task in plan["tasks"]))


def test_session_target_strips_mcagent_reply_handoff_noise() -> None:
    question = (
        "根据 MCagent 返回的乌托邦整合包资料缺口，从公开网络采集对应的整合包介绍、"
        "模组列表、特色玩法、相关教程等，准备补入 MCagent 本地资料库"
    )
    summary = {
        "collection_target": question,
        "delivery_target": "MCagent/RAG",
    }
    assert_equal("target", _session_target_hint(summary), "乌托邦整合包")
    assert_equal("collection_target", _collection_target_hint(question), "乌托邦整合包")


def test_session_target_extracts_entity_from_mcagent_reported_gap() -> None:
    question = "根据MCagent报告的乌托邦整合包缺失资料，从网上采集相关 Minecraft 整合包信息，用于补充本地资料库"
    summary = {
        "collection_target": question,
        "delivery_target": "MCagent/RAG",
    }
    assert_equal("target", _session_target_hint(summary), "乌托邦整合包")
    assert_equal("collection_target", _collection_target_hint(question), "乌托邦整合包")


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


def test_selected_crawler_action_plan_materializes_mcagent_context_first() -> None:
    summary = utopia_session_summary() | {
        "selected_action_plan": [
            {"step": 1, "tool": "mcagent_context", "goal": "ask MCagent/RAG for local evidence and gaps"},
            {"step": 2, "tool": "delegate_crawler", "goal": "collect public evidence for MCagent/RAG"},
        ],
        "planning_instruction": "Execute the CrawlerAgent-selected action_plan inside the background Crawler job.",
    }
    raw = {
        "topic": "乌托邦整合包",
        "delivery_target": "MCagent/RAG",
        "sources": ["browser_collect"],
        "tasks": [
            {
                "source": "browser_collect",
                "query": "根据 MCagent/RAG 对乌托邦整合包的缺口评估收集资料",
                "reason": "generic browser collection",
                "priority": 120,
            }
        ],
    }
    plan = _sanitize_plan(raw, utopia_question(), ROOT / "data" / "crawler_exports", max_tasks=8, session_summary=summary)
    tasks = plan["tasks"]
    assert_true("has_tasks", bool(tasks))
    assert_equal("first_task", tasks[0]["source"], "mcagent_context")
    assert_true("selected_marker", bool(tasks[0].get("from_selected_action_plan")))


def test_gap_summary_handoff_does_not_reinsert_duplicate_mcagent_context() -> None:
    raw = {
        "topic": "乌托邦整合包",
        "delivery_target": "MCagent/RAG",
        "sources": ["web_discovery", "mcmod"],
        "tasks": [
            {"source": "web_discovery", "query": "乌托邦探险之旅 玩法攻略", "reason": "public web", "priority": 90},
            {"source": "mcmod", "query": "乌托邦探险之旅", "reason": "mcmod page", "priority": 80},
        ],
    }
    summary = utopia_session_summary() | {
        "mcagent_gap_summary": "本地资料库目前有入口页，但还缺玩法路线和任务说明。",
        "mcagent_context_reply": "MCagent replied through the From-Content-To bus with local gaps and evidence.",
        "handoff_brief": "MCagent already sent inventory gaps through AgentMessage.",
        "delivery_target": "MCagent/RAG",
    }
    plan = _sanitize_plan(raw, utopia_question(), ROOT / "data" / "crawler_exports", max_tasks=8, session_summary=summary)
    sources = [task["source"] for task in plan["tasks"]]
    assert_true("no_duplicate_mcagent_context", "mcagent_context" not in sources)
    assert_true("keeps_external_collection", any(source in {"web_discovery", "mcmod"} for source in sources))


def test_local_inventory_text_does_not_replace_crawler_mcagent_context_step() -> None:
    question = "现在乌托邦整合包你本地还缺哪些资料，列出来，然后让 Crawler 去补充。"
    session_summary = {
        "delivery_target": "MCagent/RAG",
        "requested_by": "mcagent",
        "collection_target": "补充乌托邦探险之旅整合包的缺失内部资料，包括模组列表、任务线、Boss、机制、攻略、版本信息。",
        "task_goal": "补充乌托邦探险之旅整合包的缺失内部资料并交付给 MCagent/RAG。",
        "known_context": "本地资料库目前有 10706 篇已入库文档。整合包约 311 篇，乌托邦已有入口页，但还需要进一步确认缺口。",
        "selected_action_plan": [
            {"step": 1, "tool": "mcagent_context", "goal": "询问 MCagent/RAG 本地已有证据和缺口"},
            {"step": 2, "tool": "delegate_crawler", "goal": "启动后台采集并交付给 MCagent/RAG"},
        ],
    }
    plan = _sanitize_plan(
        {
            "topic": "乌托邦探险之旅",
            "delivery_target": "MCagent/RAG",
            "sources": ["mcagent_context", "web_discovery", "mcmod"],
            "tasks": [
                {"source": "mcagent_context", "query": "乌托邦探险之旅整合包缺失资料", "reason": "ask gaps", "priority": 100},
                {"source": "web_discovery", "query": "乌托邦探险之旅 整合包 模组 任务 Boss 机制 攻略", "reason": "public evidence", "priority": 90},
                {"source": "mcmod", "query": "乌托邦探险之旅", "reason": "MC百科页面", "priority": 80},
            ],
        },
        question,
        ROOT / "data" / "crawler_exports",
        max_tasks=8,
        session_summary=session_summary,
    )
    assert_equal("first_source", plan["tasks"][0]["source"], "mcagent_context")
    assert_true("selected_action_preserved", bool(plan["tasks"][0].get("from_selected_action_plan")))


def test_inventory_discovered_gap_phrase_does_not_become_target_prefix() -> None:
    question = "请根据本地库存检查发现的乌托邦整合包资料缺口，采集补充这些缺失的信息，包括但不限于整合包模组列表、特色玩法、任务线、Boss、版本特点、攻略指南等，并更新到本地 Minecraft 知识库"
    session_summary = {
        "delivery_target": "MCagent/RAG",
        "requested_by": "mcagent",
        "collection_target": question,
        "task_goal": question,
        "selected_action_plan": [
            {"step": 1, "tool": "mcagent_context", "goal": "询问 MCagent/RAG 本地已有证据和缺口"},
            {"step": 2, "tool": "delegate_crawler", "goal": "启动后台采集并交付给 MCagent/RAG"},
        ],
    }
    assert_equal("target_hint", _session_target_hint(session_summary), "乌托邦整合包")
    plan = _sanitize_plan(
        {
            "topic": "发现的乌托邦",
            "delivery_target": "MCagent/RAG",
            "tasks": [
                {"source": "mcagent_context", "query": "发现的乌托邦", "reason": "ask gaps", "priority": 100},
                {"source": "web_discovery", "query": "发现的乌托邦 模组列表", "reason": "public evidence", "priority": 90},
            ],
        },
        question,
        ROOT / "data" / "crawler_exports",
        max_tasks=6,
        session_summary=session_summary,
    )
    assert_equal("topic", plan["topic"], "乌托邦整合包")
    assert_true("no_discovered_prefix", all("发现的乌托邦" not in str(task.get("query") or "") for task in plan["tasks"]))


def test_gap_collection_with_archive_goal_still_asks_mcagent_first() -> None:
    question = "问下MCAgent乌托邦整合包还缺哪些东西 你去网上找补给他"
    summary = utopia_session_summary() | {
        "collection_target": "乌托邦整合包 乌托邦探险之旅 / Utopian Journey MC 1.20.1 Fabric",
        "task_goal": "先询问 MCagent/RAG 本地缺口，再补齐整合包完整资料：下载/包体线索、manifest、完整模组列表、玩法路线。",
        "planning_instruction": "CrawlerAgent should run mcagent_context as the first internal task, then collect public web data.",
        "delivery_target": "MCagent/RAG",
    }
    plan = plan_crawler_tasks_rule_fallback(
        question,
        ROOT / "data" / "crawler_exports",
        max_tasks=8,
        planner_error="unit timeout",
        session_summary=summary,
    )
    sources = [task["source"] for task in plan["tasks"]]
    assert_equal("first_source", sources[0], "mcagent_context")
    assert_true("keeps_archive_route", "modpack_download" in sources)
    assert_true("context_before_download", sources.index("mcagent_context") < sources.index("modpack_download"))


def test_gap_collection_without_archive_goal_does_not_schedule_modpack_download() -> None:
    question = "现在乌托邦整合包你本地还缺哪些资料，列出来，然后让 Crawler 去补充。"
    summary = utopia_session_summary() | {
        "collection_target": question,
        "task_goal": "补充乌托邦探险之旅的整合包介绍、模组列表、配置说明、玩法攻略。",
        "mcagent_gap_summary": "本地只有入口页，缺少整合包介绍、模组列表、配置说明、玩法攻略。",
        "delivery_target": "MCagent/RAG",
    }
    plan = plan_crawler_tasks_rule_fallback(
        question,
        ROOT / "data" / "crawler_exports",
        max_tasks=8,
        planner_error="unit timeout",
        session_summary=summary,
    )
    sources = [task["source"] for task in plan["tasks"]]
    assert_true(f"no_modpack_download_for_plain_gap_fill: {sources}", "modpack_download" not in sources)
    assert_true("topic_is_real_entity", "乌托邦" in plan["topic"])
    assert_true("target_is_real_entity", "乌托邦" in plan["target_hint"])
    queries = [task["query"] for task in plan["tasks"]]
    assert_true("no_relation_phrase_target", all(not query.startswith(("你本地", "我本地", "本地资料", "Crawler")) for query in queries))
    assert_true("queries_bound_to_real_target", any("乌托邦" in query for query in queries))


def test_plain_local_gap_phrase_extracts_modpack_entity_not_relation_phrase() -> None:
    question = "现在乌托邦整合包你本地还缺哪些资料，列出来，然后让 Crawler 去补充。"
    summary = {
        "delivery_target": "MCagent/RAG",
        "requested_by": "user_via_mcagent",
        "collection_target": question,
        "task_goal": question,
        "mcagent_gap_summary": "本地已有入口页，还缺玩法路线、模组列表和配置说明。",
    }
    assert_equal("target", _session_target_hint(summary), "乌托邦整合包")
    plan = plan_crawler_tasks_rule_fallback(
        question,
        ROOT / "data" / "crawler_exports",
        max_tasks=8,
        planner_error="planner timeout",
        session_summary=summary,
    )
    assert_equal("topic", plan["topic"], "乌托邦整合包")
    assert_true("no_crawler_target", all(task["query"] != "Crawler" for task in plan["tasks"]))
    assert_true("no_relation_query", all("你本地" not in task["query"] for task in plan["tasks"]))


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


def test_sanitized_plan_limits_slow_mcmod_fanout() -> None:
    raw = {
        "topic": "乌托邦整合包",
        "delivery_target": "MCagent/RAG",
        "sources": ["mcmod", "web_discovery", "playwright"],
        "tasks": [
            {"source": "mcmod", "query": f"乌托邦探险之旅 资料 {index}", "reason": "mcmod coverage", "priority": 100 - index}
            for index in range(8)
        ]
        + [
            {"source": "web_discovery", "query": "乌托邦探险之旅 玩法攻略", "reason": "public web", "priority": 90},
            {"source": "playwright", "query": "https://www.mcmod.cn/modpack/1337.html", "reason": "render core page", "priority": 88},
        ],
    }
    plan = _sanitize_plan(
        raw,
        utopia_question(),
        ROOT / "data" / "crawler_exports",
        max_tasks=8,
        session_summary=utopia_session_summary(),
    )
    tasks = plan["tasks"]
    mcmod_count = sum(1 for task in tasks if task["source"] == "mcmod")
    assert_true("mcmod_fanout_limited", mcmod_count <= 2)
    assert_true("keeps_generic_sources", any(task["source"] == "web_discovery" for task in tasks) and any(task["source"] == "playwright" for task in tasks))


def test_general_domain_fallback_does_not_default_to_minecraft_tools() -> None:
    question = (
        "\u5e2e\u6211\u91c7\u96c6 Python requests \u5e93\u7684\u5b98\u65b9\u6587\u6863\u3001"
        "GitHub \u4ed3\u5e93\u3001\u6700\u65b0\u53d1\u5e03\u8bf4\u660e\u548c"
        "\u5e38\u89c1\u7528\u6cd5\uff0c\u4fdd\u5b58\u7ed9\u901a\u7528 RAG \u4f7f\u7528\u3002"
    )
    plan = plan_crawler_tasks_rule_fallback(
        question,
        ROOT / "data" / "crawler_exports",
        max_tasks=8,
        planner_error="unit timeout",
        session_summary={
            "delivery_target": "RAG",
            "requested_by": "user",
            "collection_target": question,
            "task_goal": question,
        },
    )
    sources = [task["source"] for task in plan["tasks"]]
    assert_true("has_general_sources", any(source in {"web_discovery", "playwright", "fetch_url"} for source in sources))
    assert_true("no_minecraft_sources", all(source not in {"mcmod", "modrinth", "modpack_download", "modpack_internal", "mediawiki", "ftbwiki", "createwiki"} for source in sources))
    assert_equal("package_type", plan["package_type"], "unknown")
    assert_true("general_coverage", any("source ecosystem" in goal or "official" in goal for goal in plan["coverage_goals"]))


def test_general_domain_sanitize_filters_minecraft_tool_noise() -> None:
    question = "采集 Playwright Python 的官方安装文档、API 文档、GitHub releases 和浏览器自动化示例。"
    raw = {
        "topic": "Playwright Python",
        "package_type": "unknown",
        "delivery_target": "human",
        "sources": ["mcmod", "modrinth", "web_discovery", "playwright"],
        "tasks": [
            {"source": "mcmod", "query": "Playwright Python", "reason": "bad domain carryover", "priority": 150},
            {"source": "modrinth", "query": "Playwright Python", "reason": "bad domain carryover", "priority": 140},
            {"source": "web_discovery", "query": "Playwright Python official docs", "reason": "official docs", "priority": 120},
            {"source": "playwright", "query": "Playwright Python GitHub releases", "reason": "render release pages", "priority": 115},
        ],
    }
    plan = _sanitize_plan(raw, question, ROOT / "data" / "crawler_exports", max_tasks=8, session_summary={"delivery_target": "human"})
    sources = [task["source"] for task in plan["tasks"]]
    assert_true("keeps_general_task", "web_discovery" in sources and "playwright" in sources)
    assert_true("filters_minecraft_tasks", all(source not in {"mcmod", "modrinth", "modpack_download", "modpack_internal"} for source in sources))
    assert_true("filters_source_list", all(source not in {"mcmod", "modrinth", "modpack_download", "modpack_internal"} for source in plan["sources"]))


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


def test_reflection_llm_failure_continues_existing_pending_task() -> None:
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
    assert_equal("action", decision["action"], "execute_pending")
    assert_equal("selected_index", decision["selected_index"], 0)
    assert_true("no_tasks", decision["tasks"] == [])
    assert_true("reflection_issue", "reflection_llm_error" in decision["contract"]["issues"])
    assert_true("continued_existing", "continued_with_existing_pending_task" in decision["contract"]["issues"])
    assert_true("rate_limit_visible", "429" in decision["reason"])


def test_archive_guard_does_not_force_download_when_mcagent_context_has_archive_evidence() -> None:
    original_client = crawler_llm_planner._planner_client

    class FakeClient:
        def chat(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return '{"action":"execute_pending","selected_index":1,"reason":"local archive evidence exists; continue with public web coverage","tasks":[]}'

    crawler_llm_planner._planner_client = lambda: (FakeClient(), "fake-reflect")  # type: ignore[assignment]
    try:
        decision = reflect_crawler_progress(
            "问下MCAgent乌托邦整合包还缺哪些东西 你去网上找补给他",
            {"topic": "乌托邦整合包", "target_hint": "乌托邦整合包", "delivery_target": "MCagent/RAG"},
            task_results=[
                {
                    "source": "mcagent_context",
                    "query": "乌托邦整合包",
                    "returncode": 0,
                    "output": "MCagent/RAG local context collected.",
                    "manifest_stats": {"records": 1, "downloads": 0},
                    "artifact_refs": [
                        {
                            "title": "Downloaded modpack archive evidence: MinePIxelWuTuoBang3.5.1Fix.zip",
                            "path": "D:\\magic\\MC_Agent\\data\\crawler_exports\\modpack_download\\downloaded_archive_evidence_1.md",
                        }
                    ],
                }
            ],
            pending_tasks=[
                {"source": "modpack_download", "query": "乌托邦整合包", "reason": "find archive", "priority": 210},
                {"source": "web_discovery", "query": "乌托邦整合包 玩法路线", "reason": "fill web coverage", "priority": 120},
            ],
            session_summary={
                "delivery_target": "MCagent/RAG",
                "collection_target": "乌托邦整合包完整资料：下载/包体线索、manifest、完整模组列表、玩法路线。",
            },
            max_new_tasks=4,
        )
    finally:
        crawler_llm_planner._planner_client = original_client  # type: ignore[assignment]
    assert_equal("action", decision["action"], "execute_pending")
    assert_equal("selected_index", decision["selected_index"], 1)
    assert_true("used_llm_reflection", "archive_goal_guard" not in decision["planner"])


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


def test_mcagent_gap_fill_request_does_not_force_archive_download_first() -> None:
    question = "问下MCAgent乌托邦整合包还缺哪些东西 你去网上找补给他"
    plan = plan_crawler_tasks_rule_fallback(
        question,
        ROOT / "data" / "crawler_exports",
        max_tasks=8,
        planner_error="unit timeout",
        session_summary={
            "delivery_target": "MCagent/RAG",
            "requested_by": "user",
            "collection_target": question,
            "planning_instruction": "CrawlerAgent should run mcagent_context as the first internal task, read MCagent/RAG local evidence and gaps, then collect public web data that fills those gaps and deliver usable artifacts to MCagent/RAG.",
        },
    )
    sources = [task["source"] for task in plan["tasks"]]
    assert_equal("first_source", sources[0], "mcagent_context")
    assert_true(f"does_not_force_archive_first: {sources}", "modpack_download" not in sources[:2])
    assert_true(f"keeps_public_sources: {sources}", any(source in {"web_discovery", "playwright", "modrinth", "mcmod", "topic_discovery"} for source in sources))


def test_mcagent_context_archive_evidence_does_not_create_archive_intent() -> None:
    question = "问下MCAgent乌托邦整合包还缺哪些东西 你去网上找补给他"
    summary = {
        "delivery_target": "MCagent/RAG",
        "requested_by": "user",
        "collection_target": "根据MCagent关于乌托邦整合包的知识缺口分析，从公开网络来源全面采集缺失资料，整理后交付给MCagent用于补充本地RAG知识库。",
        "planning_instruction": "先运行 mcagent_context，然后用公开网页、项目页、论坛资料补齐缺口。",
        "mcagent_gap_summary": (
            "本地已有证据包括 D:\\magic\\MC_Agent\\data\\crawler_exports\\modpack_download\\20260528\\modpack_archive_discovery.md，"
            "摘要里出现 .mrpack、.zip、pack_archive 和 downloaded_archive_evidence；"
            "建议下一步补充官方/项目页、完整模组列表、版本与更新日志、新手路线/教程。"
        ),
    }
    plan = plan_crawler_tasks_rule_fallback(
        question,
        ROOT / "data" / "crawler_exports",
        max_tasks=8,
        planner_error="unit timeout",
        session_summary=summary,
    )
    sources = [task["source"] for task in plan["tasks"]]
    assert_equal("first_source", sources[0], "mcagent_context")
    assert_true(f"no_archive_forced_by_context: {sources}", "modpack_download" not in sources[:2])


def test_llm_plan_drops_modpack_internal_without_real_archive_input() -> None:
    question = "问下MCAgent乌托邦整合包还缺哪些东西 你去网上找补给他"
    plan = _sanitize_plan(
        {
            "topic": "乌托邦整合包",
            "target_hint": "乌托邦整合包",
            "delivery_target": "MCagent/RAG",
            "sources": ["mcagent_context", "modpack_internal", "web_discovery"],
            "tasks": [
                {"source": "mcagent_context", "query": "乌托邦整合包", "reason": "check gaps", "priority": 200},
                {"source": "modpack_internal", "query": "乌托邦整合包", "reason": "parse internals too early", "priority": 180},
                {"source": "web_discovery", "query": "乌托邦探险之旅 模组列表", "reason": "public evidence", "priority": 120},
            ],
        },
        question,
        ROOT / "data" / "crawler_exports",
        max_tasks=8,
        session_summary={
            "delivery_target": "MCagent/RAG",
            "requested_by": "user",
            "collection_target": "从公开网络来源全面采集乌托邦整合包缺失资料，整理后交付给MCagent用于补充本地RAG知识库。",
            "mcagent_gap_summary": "历史资料摘要提到 .zip/.mrpack，但没有提供本轮可用 archive_path。",
        },
    )
    sources = [task["source"] for task in plan["tasks"]]
    assert_true(f"no_unqualified_internal: {sources}", "modpack_internal" not in sources)
    assert_true("keeps_web_discovery", "web_discovery" in sources)


def test_structured_browser_plan_extracts_output_dir_from_question() -> None:
    question = (
        "用 Crawler 打开 https://webscraper.io/test-sites/e-commerce/static/computers/laptops "
        "提取前 5 个商品的名称、价格、链接，保存为 xlsx、csv、json 到 "
        "data/manual_tests/live_five_direction_products/case_d5。"
    )
    plan = _sanitize_plan(
        {
            "topic": "WebScraper laptops",
            "package_type": "product",
            "delivery_target": "human",
            "sources": ["browser_collect"],
            "tasks": [
                {
                    "source": "browser_collect",
                    "query": "https://webscraper.io/test-sites/e-commerce/static/computers/laptops",
                    "reason": "collect structured rows",
                    "priority": 120,
                }
            ],
        },
        question,
        ROOT / "data" / "crawler_exports",
        max_tasks=4,
        session_summary={"delivery_target": "human", "collection_target": question, "task_goal": question},
    )
    task = plan["tasks"][0]
    assert_equal("source", task["source"], "browser_collect")
    assert_equal("output_dir", task["output_dir"], "data/manual_tests/live_five_direction_products/case_d5")
    assert_equal("max_items", task["max_items"], 5)
    assert_equal("fields", task["fields"], ["name", "price", "link"])


def test_reflection_does_not_force_archive_from_mcagent_context_summary() -> None:
    original_client = crawler_llm_planner._planner_client

    class FakeClient:
        def chat(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return '{"action":"execute_pending","selected_index":0,"reason":"continue with public web evidence","tasks":[]}'

    crawler_llm_planner._planner_client = lambda: (FakeClient(), "fake-reflect")  # type: ignore[assignment]
    try:
        decision = reflect_crawler_progress(
            "问下MCAgent乌托邦整合包还缺哪些东西 你去网上找补给他",
            {"topic": "乌托邦", "target_hint": "乌托邦", "delivery_target": "MCagent/RAG"},
            task_results=[
                {
                    "source": "mcagent_context",
                    "returncode": 0,
                    "mcagent_gap_summary": "本地证据摘要提到历史 .zip/.mrpack 路径，但本轮只要求补公开文字资料。",
                }
            ],
            pending_tasks=[
                {"source": "web_discovery", "query": "乌托邦探险之旅 模组列表", "reason": "fill public evidence", "priority": 120},
                {"source": "modpack_download", "query": "乌托邦 modpack .mrpack .zip", "reason": "candidate archive route", "priority": 80},
            ],
            session_summary={
                "delivery_target": "MCagent/RAG",
                "requested_by": "user",
                "collection_target": "根据MCagent关于乌托邦整合包的知识缺口分析，从公开网络来源全面采集缺失资料，整理后交付给MCagent用于补充本地RAG知识库。",
                "mcagent_gap_summary": "本地证据摘要提到历史 .zip/.mrpack 路径和 modpack_archive_discovery，但不是当前用户要求。",
            },
            max_new_tasks=4,
        )
    finally:
        crawler_llm_planner._planner_client = original_client  # type: ignore[assignment]
    assert_equal("action", decision["action"], "execute_pending")
    assert_equal("selected_index", decision["selected_index"], 0)
    assert_true("not_archive_goal_guard", "archive_goal_guard" not in str(decision.get("planner") or ""))


def test_reflection_after_mcagent_gap_reply_lets_crawler_llm_choose_public_web() -> None:
    original_client = crawler_llm_planner._planner_client

    class FakeClient:
        def chat(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return '{"action":"execute_pending","selected_index":1,"reason":"MCagent asked for public pages, mod list, gameplay route, and changelog before package probing","tasks":[]}'

    crawler_llm_planner._planner_client = lambda: (FakeClient(), "fake-reflect")  # type: ignore[assignment]
    try:
        decision = reflect_crawler_progress(
            "问下MCAgent乌托邦整合包还缺哪些东西 你去网上找补给他",
            {
                "topic": "乌托邦整合包信息补全",
                "target_hint": "乌托邦整合包",
                "delivery_target": "MCagent/RAG",
                "question": "乌托邦整合包缺失的信息：先通过 mcagent_context 确定当前 MCagent/RAG 中乌托邦整合包已有的资料和缺失项，然后从网上采集这些缺失内容，交付给 MCagent/RAG",
                "reason": "先通过mcagent_context定位缺口，再用browser_collect和mcmod采集缺失内容并交付",
                "coverage_goals": ["获取乌托邦整合包完整资料", "补全模组列表和描述", "收集安装和配置信息"],
            },
            task_results=[
                {
                    "source": "mcagent_context",
                    "query": "乌托邦整合包",
                    "returncode": 0,
                    "manifest_stats": {"records": 1, "downloads": 0, "candidates": 0},
                    "output": "MCagent/RAG local context collected through AgentMessage bus.",
                    "agent_message_exchange": {
                        "reply": {
                            "from_agent": "MCagent",
                            "to_agent": "CrawlerAgent",
                            "content": (
                                "本地已有证据候选包括历史 Downloaded modpack archive evidence 和 local_archive_path；"
                                "可能仍需 CrawlerAgent 自行核查/补充：公开项目页、版本/下载页、完整模组列表、"
                                "任务线/玩法路线、更新日志或配置说明。"
                            ),
                        }
                    },
                    "topic_validation": {
                        "next_action": "从网上采集乌托邦探险之旅整合包的完整模组列表、描述、安装配置信息，并交付给MCagent补充资料库。"
                    },
                }
            ],
            pending_tasks=[
                {
                    "source": "modpack_download",
                    "query": "乌托邦整合包",
                    "reason": "User explicitly requires a fully automatic public modpack archive route before internal parsing; collect objective download facts first.",
                    "priority": 210,
                },
                {"source": "web_discovery", "query": "乌托邦整合包 完整模组列表", "reason": "collect public page evidence", "priority": 140},
                {"source": "playwright", "query": "乌托邦整合包 玩法路线 更新日志", "reason": "render project pages", "priority": 136},
            ],
            session_summary={
                "delivery_target": "MCagent/RAG",
                "requested_by": "user",
                "collection_target": "为 MCagent 补充乌托邦整合包缺失的信息：先通过 mcagent_context 确定当前 MCagent/RAG 中乌托邦整合包已有的资料和缺失项，然后从网上采集这些缺失内容，交付给 MCagent/RAG。",
                "planning_instruction": "CrawlerAgent should run mcagent_context as the first internal task, read MCagent/RAG local evidence and gaps, then collect public web data that fills those gaps and deliver usable artifacts to MCagent/RAG.",
                "selected_action_plan": [
                    {"step": 1, "tool": "mcagent_context", "goal": "询问 MCagent/RAG 本地已有证据和缺口"},
                    {"step": 2, "tool": "delegate_crawler", "goal": "采集缺失内容并交付 MCagent/RAG"},
                ],
            },
            max_new_tasks=4,
        )
    finally:
        crawler_llm_planner._planner_client = original_client  # type: ignore[assignment]
    assert_equal("action", decision["action"], "execute_pending")
    assert_equal("selected_index", decision["selected_index"], 1)
    assert_true("llm_reflection_used", decision.get("planner") == "fake-reflect")
    assert_true("not_archive_goal_guard", "archive_goal_guard" not in str(decision.get("planner") or ""))


def test_reflection_explicit_archive_goal_still_uses_crawler_llm_not_guard() -> None:
    original_client = crawler_llm_planner._planner_client

    class FakeClient:
        def chat(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return '{"action":"execute_pending","selected_index":0,"reason":"CrawlerAgent chooses archive probing from the objective pending task list","tasks":[]}'

    crawler_llm_planner._planner_client = lambda: (FakeClient(), "fake-reflect")  # type: ignore[assignment]
    try:
        decision = reflect_crawler_progress(
            "找到 Craftoria 整合包公开全自动 .mrpack 或 .zip 包体下载路线，下载并解析 manifest",
            {"topic": "Craftoria", "target_hint": "Craftoria", "delivery_target": "MCagent/RAG"},
            task_results=[{"source": "web_discovery", "query": "Craftoria", "returncode": 0, "empty_result": True}],
            pending_tasks=[
                {"source": "modpack_download", "query": "Craftoria modpack .mrpack .zip", "reason": "public archive candidate probing", "priority": 210},
                {"source": "web_discovery", "query": "Craftoria modpack download page", "reason": "public project pages", "priority": 130},
            ],
            session_summary={"delivery_target": "MCagent/RAG", "collection_target": "Craftoria 整合包公开全自动 .mrpack 或 .zip 包体下载路线"},
            max_new_tasks=4,
        )
    finally:
        crawler_llm_planner._planner_client = original_client  # type: ignore[assignment]
    assert_equal("action", decision["action"], "execute_pending")
    assert_equal("selected_index", decision["selected_index"], 0)
    assert_true("llm_reflection_used", decision.get("planner") == "fake-reflect")


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


def test_sanitize_plan_rejects_tasks_that_follow_crawler_word_instead_of_target() -> None:
    question = "\u73b0\u5728\u4e4c\u6258\u90a6\u6574\u5408\u5305\u4f60\u672c\u5730\u8fd8\u7f3a\u54ea\u4e9b\u8d44\u6599\uff0c\u5217\u51fa\u6765\uff0c\u7136\u540e\u8ba9 Crawler \u53bb\u8865\u5145\u3002"
    session_summary = {
        "delivery_target": "MCagent/RAG",
        "collection_target": "\u4e4c\u6258\u90a6\u6574\u5408\u5305",
        "mcagent_gap_summary": "\u7f3a\u73a9\u6cd5\u8def\u7ebf\u3001\u4efb\u52a1\u7ebf\u3001\u6a21\u7ec4\u5217\u8868\u3001\u4e0b\u8f7d\u9875\u3001\u7248\u672c\u4fe1\u606f\uff0c\u9700\u8981\u516c\u5f00\u53ef\u5f15\u7528\u8d44\u6599\u3002",
    }
    plan = _sanitize_plan(
        {
            "topic": "web crawler introduction",
            "package_type": "general",
            "delivery_target": "MCagent/RAG",
            "subqueries": ["web crawler introduction", "popular open source crawlers"],
            "sources": ["browser_collect", "read_local_file", "search_local_files", "web_discovery"],
            "tasks": [
                {"source": "browser_collect", "query": "web crawler introduction", "reason": "bad", "priority": 120},
                {"source": "read_local_file", "query": "popular open source crawlers", "reason": "bad", "priority": 108},
            ],
        },
        question,
        ROOT / "data" / "crawler_exports",
        6,
        session_summary=session_summary,
    )
    assert_equal("topic", plan["topic"], "\u4e4c\u6258\u90a6\u6574\u5408\u5305")
    queries = [str(task.get("query") or "") for task in plan["tasks"]]
    assert_true("no_web_crawler_topic", all("web crawler" not in query.lower() and "open source crawlers" not in query.lower() for query in queries))
    assert_true("queries_stay_on_target", all("\u4e4c\u6258\u90a6" in query or query.startswith("http") for query in queries))


def test_sanitize_plan_drops_local_file_tasks_without_objective_path() -> None:
    question = "Collect Farmer's Delight public web evidence for MCagent/RAG."
    plan = _sanitize_plan(
        {
            "topic": "Farmer's Delight",
            "package_type": "mod",
            "delivery_target": "MCagent/RAG",
            "sources": ["read_local_file", "search_local_files", "web_discovery", "playwright"],
            "tasks": [
                {"source": "read_local_file", "query": "Farmer's Delight local notes", "reason": "bad local guess", "priority": 130},
                {"source": "search_local_files", "query": "Farmer's Delight", "reason": "bad local guess", "priority": 120},
                {"source": "web_discovery", "query": "Farmer's Delight Minecraft mod", "reason": "public web", "priority": 110},
            ],
        },
        question,
        ROOT / "data" / "crawler_exports",
        max_tasks=6,
        session_summary={
            "delivery_target": "MCagent/RAG",
            "requested_by": "user_via_mcagent",
            "task_goal": question,
            "collection_target": "Farmer's Delight",
        },
    )
    plan_sources = [str(task.get("source") or "") for task in plan["tasks"]]
    assert_true("no_unqualified_local_tools", "read_local_file" not in plan_sources and "search_local_files" not in plan_sources)
    assert_true("keeps_public_collection", any(source in {"web_discovery", "playwright", "mcmod", "modrinth"} for source in plan_sources))


def test_general_chinese_collection_extracts_entity_not_delivery_phrase() -> None:
    question = (
        "\u8bf7\u83b7\u53d6\u519c\u592b\u4e50\u4e8b Farmer's Delight "
        "\u7684\u516c\u5f00\u57fa\u7840\u8d44\u6599\u5e76\u4ea4\u7ed9 MCagent/RAG \u4f7f\u7528\u3002"
        "\u91c7\u96c6\u91cd\u70b9\u662f\u9879\u76ee\u4ecb\u7ecd\u3001\u7248\u672c/\u4e0b\u8f7d\u9875\u7ebf\u7d22\u3001\u73a9\u6cd5\u5165\u95e8\u548c\u53ef\u9760\u6765\u6e90\u3002"
    )
    assert_equal("collection_target", _collection_target_hint(question), "\u519c\u592b\u4e50\u4e8b Farmer's Delight")
    plan = _sanitize_plan(
        {
            "topic": "\u519c\u592b\u4e50\u4e8b Farmer's Delight \u7684\u516c\u5f00\u57fa\u7840\u8d44\u6599\u5e76\u4ea4\u7ed9 MCagent/RAG \u4f7f\u7528",
            "delivery_target": "MCagent/RAG",
            "tasks": [
                {
                    "source": "web_discovery",
                    "query": "\u519c\u592b\u4e50\u4e8b Farmer's Delight \u7684\u516c\u5f00\u57fa\u7840\u8d44\u6599\u5e76\u4ea4\u7ed9 MCagent/RAG \u4f7f\u7528",
                    "reason": "LLM copied full request",
                    "priority": 90,
                }
            ],
        },
        question,
        ROOT / "data" / "crawler_exports",
        max_tasks=4,
        session_summary={"delivery_target": "MCagent/RAG", "requested_by": "user_via_mcagent", "collection_target": question},
    )
    assert_equal("target_hint", plan["target_hint"], "\u519c\u592b\u4e50\u4e8b Farmer's Delight")
    assert_equal("topic", plan["topic"], "\u519c\u592b\u4e50\u4e8b Farmer's Delight")
    assert_true("no_delivery_phrase", all("MCagent/RAG" not in str(task.get("query") or "") for task in plan["tasks"]))


def test_planner_client_uses_bounded_timeout_for_agent_planning() -> None:
    calls: list[tuple[str, float, int]] = []
    original_client_for_agent = crawler_llm_planner.client_for_agent

    class FakeClient:
        pass

    def fake_client_for_agent(config, agent, *, temperature=0.0, timeout_seconds=None):  # noqa: ANN001, ANN202
        calls.append((agent, temperature, int(timeout_seconds or 0)))
        return FakeClient(), "fake"

    crawler_llm_planner.client_for_agent = fake_client_for_agent  # type: ignore[assignment]
    try:
        crawler_llm_planner._planner_client()
    finally:
        crawler_llm_planner.client_for_agent = original_client_for_agent  # type: ignore[assignment]
    assert_equal("planner_agent", calls[0][0], "crawler_agent")
    assert_equal("planner_timeout", calls[0][2], crawler_llm_planner.PLANNER_LLM_TIMEOUT_SECONDS)


def test_job_planner_default_startup_timeout_is_bounded() -> None:
    assert_true(
        f"startup_timeout_bounded: {web_server.DEFAULT_CRAWLER_PLANNER_TIMEOUT_SECONDS}",
        crawler_llm_planner.PLANNER_LLM_TIMEOUT_SECONDS
        < web_server.DEFAULT_CRAWLER_PLANNER_TIMEOUT_SECONDS
        <= crawler_llm_planner.PLANNER_LLM_TIMEOUT_SECONDS + 30,
    )


def test_planner_json_chat_requests_json_object_mode() -> None:
    calls: list[dict] = []

    class FakeClient:
        def chat(self, messages, temperature=None, max_tokens=None, response_format=None):  # noqa: ANN001, ANN202
            calls.append({"messages": messages, "temperature": temperature, "max_tokens": max_tokens, "response_format": response_format})
            return '{"ok": true}'

    text = crawler_llm_planner._planner_json_chat(FakeClient(), [{"role": "user", "content": "Return JSON"}], max_tokens=123)
    assert_equal("json_text", text, '{"ok": true}')
    assert_equal("response_format", calls[0]["response_format"], {"type": "json_object"})
    assert_equal("max_tokens", calls[0]["max_tokens"], 123)


def test_sanitize_plan_accepts_llm_priority_labels() -> None:
    plan = _sanitize_plan(
        {
            "topic": "乌托邦整合包",
            "delivery_target": "MCagent/RAG",
            "tasks": [
                {"source": "mcagent_context", "query": "乌托邦整合包", "reason": "check local gaps", "priority": "high"},
                {"source": "web_discovery", "query": "乌托邦探险之旅 玩法", "reason": "collect public guide", "priority": "medium"},
            ],
        },
        utopia_question(),
        ROOT / "data" / "crawler_exports",
        max_tasks=4,
        session_summary=utopia_session_summary(),
    )
    assert_equal("strategy", plan["strategy"], "crawler_llm_planner")
    assert_true("has_tasks", len(plan["tasks"]) >= 2)
    assert_true("priority_numeric", all(isinstance(task.get("priority"), int) for task in plan["tasks"]))


def test_sanitize_plan_normalizes_llm_action_aliases() -> None:
    plan = _sanitize_plan(
        {
            "topic": "乌托邦整合包",
            "delivery_target": "MCagent/RAG",
            "tasks": [
                {"action": "mcagent_context", "query": "乌托邦整合包还缺哪些东西", "reason": "ask gaps", "priority": 100},
                {"action": "web_search", "query": "乌托邦探险之旅 任务线", "reason": "public evidence", "priority": 90},
            ],
        },
        utopia_question(),
        ROOT / "data" / "crawler_exports",
        max_tasks=4,
        session_summary=utopia_session_summary(),
    )
    sources = [task["source"] for task in plan["tasks"]]
    assert_equal("first_source", sources[0], "mcagent_context")
    assert_true("web_search_alias", "web_discovery" in sources)


def test_gap_question_text_still_keeps_mcagent_context_first() -> None:
    question = "现在乌托邦整合包你本地还缺哪些资料，列出来，然后让 Crawler 去补充"
    plan = _sanitize_plan(
        {
            "topic": "乌托邦整合包",
            "delivery_target": "MCagent/RAG",
            "tasks": [
                {"source": "web_discovery", "query": "乌托邦整合包 玩法 教程", "reason": "public evidence", "priority": 90},
            ],
        },
        question,
        ROOT / "data" / "crawler_exports",
        max_tasks=4,
        session_summary={"delivery_target": "MCagent/RAG", "collection_target": question},
    )
    assert_equal("first_source", plan["tasks"][0]["source"], "mcagent_context")
    assert_equal("first_query", plan["tasks"][0]["query"], "乌托邦整合包")


def test_fallback_confirmation_finish_is_overridden_for_target_bound_pending_task() -> None:
    assert_equal(
        "continue_target_bound",
        crawler_llm_planner._fallback_confirmation_should_continue(
            {"action": "finish", "reason": "Pending tasks are generic placeholders and irrelevant."},
            [{"source": "web_discovery", "query": "乌托邦整合包 玩法 教程"}],
            {"topic": "乌托邦整合包", "target_hint": "乌托邦整合包"},
        ),
        True,
    )
    assert_equal(
        "do_not_continue_unbound",
        crawler_llm_planner._fallback_confirmation_should_continue(
            {"action": "finish", "reason": "Pending tasks are generic placeholders and irrelevant."},
            [{"source": "web_discovery", "query": "本地已有整合包"}],
            {"topic": "乌托邦整合包", "target_hint": "乌托邦整合包"},
        ),
        False,
    )


def test_rag_structured_evidence_request_does_not_force_browser_collect() -> None:
    question = (
        "根据本地盘点，乌托邦整合包（Utopian Journey）资料不足，需要从公开资源采集整合包介绍、"
        "完整模组列表、安装与配置说明、游戏玩法攻略、更新日志。目标是为MCagent/RAG提供详尽、结构化、可引用的资料。"
    )
    plan = _sanitize_plan(
        {
            "topic": "可引用的乌托邦",
            "delivery_target": "MCagent/RAG",
            "tasks": [
                {"source": "browser_collect", "query": question, "reason": "structured RAG evidence", "priority": "high"},
                {"source": "web_discovery", "query": "乌托邦探险之旅 模组列表", "reason": "public evidence", "priority": 90},
            ],
        },
        question,
        ROOT / "data" / "crawler_exports",
        max_tasks=6,
        session_summary={"delivery_target": "MCagent/RAG", "collection_target": question},
    )
    assert_true("target_real_pack", "乌托邦" in plan["target_hint"] and "可引用" not in plan["target_hint"])
    sources = [task["source"] for task in plan["tasks"]]
    assert_true("no_browser_collect", "browser_collect" not in sources)
    assert_true("has_public_collection", any(source in sources for source in ("web_discovery", "playwright", "mcmod", "modrinth")))


def test_mcagent_reply_gap_handoff_does_not_prioritize_modpack_download() -> None:
    question = (
        "根据 MCagent 返回的乌托邦整合包资料缺口，从公开网络采集对应的整合包介绍、"
        "模组列表、特色玩法、相关教程等，准备补入 MCagent 本地资料库"
    )
    plan = _sanitize_plan(
        {
            "topic": "返回的乌托邦整合包",
            "target_hint": "返回的乌托邦整合包",
            "delivery_target": "MCagent/RAG",
            "sources": ["modpack_download", "web_discovery", "playwright"],
            "tasks": [
                {"source": "modpack_download", "query": "返回的乌托邦整合包", "reason": "download route", "priority": 260},
                {"source": "web_discovery", "query": "乌托邦整合包 介绍 模组列表 玩法", "reason": "public pages", "priority": 90},
            ],
        },
        question,
        ROOT / "data" / "crawler_exports",
        max_tasks=6,
        session_summary={"delivery_target": "MCagent/RAG", "collection_target": question},
    )
    assert_equal("target_hint", plan["target_hint"], "乌托邦整合包")
    first_sources = [task["source"] for task in plan["tasks"][:3]]
    assert_equal("first_source", first_sources[0], "mcagent_context")
    assert_true("public_before_archive", "web_discovery" in first_sources or "playwright" in first_sources)
    assert_true("archive_not_second", len(first_sources) < 2 or first_sources[1] != "modpack_download")
    assert_true("no_returned_prefix", all("返回的" not in str(task.get("query") or "") for task in plan["tasks"]))


def test_mcagent_reported_gap_fallback_does_not_force_archive_first() -> None:
    question = "根据MCagent报告的乌托邦整合包缺失资料，从网上采集相关 Minecraft 整合包信息，用于补充本地资料库"
    plan = plan_crawler_tasks_rule_fallback(
        question,
        ROOT / "data" / "crawler_exports",
        max_tasks=8,
        planner_error="planner exceeded 90s startup timeout",
        session_summary={"delivery_target": "MCagent/RAG", "collection_target": question},
    )
    assert_equal("target_hint", plan["target_hint"], "乌托邦整合包")
    first_sources = [task["source"] for task in plan["tasks"][:3]]
    first_queries = [task["query"] for task in plan["tasks"][:3]]
    assert_equal("first_source", first_sources[0], "mcagent_context")
    assert_true("public_before_archive", any(source in {"web_discovery", "playwright", "mcmod", "modrinth"} for source in first_sources[1:]))
    assert_true("archive_not_second", len(first_sources) < 2 or first_sources[1] != "modpack_download")
    assert_true("no_generic_minecraft_target", all(query != "Minecraft" and query != "/ Minecraft" for query in first_queries))


def test_llm_gap_plan_does_not_inject_modpack_download_without_archive_goal() -> None:
    question = "问下MCAgent乌托邦整合包还缺哪些东西 你去网上找补给他"
    action_plan = [
        {"step": 1, "tool": "mcagent_context", "goal": "Inspect MCagent's local RAG evidence to understand what the Utopia modpack is currently missing or what gaps exist."},
        {"step": 2, "tool": "delegate_crawler", "goal": "Start a background collection loop to find and gather missing information about the Utopia modpack from the web, with delivery to MCagent/RAG for ingestion."},
    ]
    plan = _sanitize_plan(
        {
            "topic": "乌托邦整合包缺失内容收集",
            "delivery_target": "MCagent/RAG",
            "coverage_goals": ["找出乌托邦整合包缺失的模组或信息"],
            "tasks": [
                {"source": "mcagent_context", "query": "乌托邦整合包 缺失", "reason": "询问MCAgent当前已知的缺失项", "priority": 100},
                {"source": "web_discovery", "query": "乌托邦整合包 模组列表", "reason": "搜索网页获取整合包详情", "priority": 90},
                {"source": "mcmod", "query": "乌托邦", "reason": "在MC百科搜索整合包信息", "priority": 80},
            ],
            "reason": "先向MCAgent查询缺口，再通过网络和MC百科收集补充信息",
        },
        question,
        ROOT / "data" / "crawler_exports",
        max_tasks=8,
        session_summary={
            "delivery_target": "MCagent/RAG",
            "requested_by": "user",
            "collection_target": question,
            "selected_action_plan": action_plan,
        },
    )
    sources = [task["source"] for task in plan["tasks"]]
    assert_equal("target_hint", plan["target_hint"], "乌托邦整合包")
    assert_equal("first_source", sources[0], "mcagent_context")
    assert_true(f"no_modpack_download: {sources}", "modpack_download" not in sources)
    assert_true("has_public_web", any(source in {"web_discovery", "playwright"} for source in sources))


def test_pronoun_collection_target_falls_back_to_original_user_entity() -> None:
    question = "反馈的该整合包：针对MCagent/RAG反馈的该整合包资料缺口，从网络采集缺失数据并补充到本地资料库。"
    original = "问下MCAgent乌托邦整合包还缺哪些东西 你去网上找补给他"
    action_plan = [
        {"step": 1, "tool": "mcagent_context", "goal": "Inspect MCagent's local RAG evidence to understand what the Utopia modpack is currently missing or what gaps exist."},
        {"step": 2, "tool": "delegate_crawler", "goal": "Start a background collection loop to find and gather missing information about the Utopia modpack from the web, with delivery to MCagent/RAG for ingestion."},
    ]
    plan = _sanitize_plan(
        {
            "topic": "反馈的该整合包",
            "target_hint": "反馈的该整合包",
            "delivery_target": "MCagent/RAG",
            "tasks": [
                {"source": "mcagent_context", "query": "反馈的该整合包", "reason": "ask context", "priority": 100},
                {"source": "web_discovery", "query": "乌托邦整合包 模组 介绍", "reason": "public pages", "priority": 90},
            ],
        },
        question,
        ROOT / "data" / "crawler_exports",
        max_tasks=8,
        session_summary={
            "delivery_target": "MCagent/RAG",
            "collection_target": question,
            "original_user_message": original,
            "selected_action_plan": action_plan,
        },
    )
    sources = [task["source"] for task in plan["tasks"]]
    assert_equal("target_hint", plan["target_hint"], "乌托邦整合包")
    assert_true(f"no_modpack_download_from_action_plan_wording: {sources}", "modpack_download" not in sources)
    assert_true("queries_use_real_entity", all("反馈的该" not in str(task.get("query") or "") for task in plan["tasks"]))


def test_handoff_brief_archive_words_do_not_create_archive_intent() -> None:
    question = "问下MCAgent乌托邦整合包还缺哪些东西 你去网上找补给他"
    plan = _sanitize_plan(
        {
            "topic": "乌托邦整合包",
            "target_hint": "乌托邦整合包",
            "delivery_target": "MCagent/RAG",
            "sources": ["web_discovery", "playwright", "mcmod", "modrinth"],
            "tasks": [
                {"source": "mcagent_context", "query": "乌托邦整合包", "reason": "check local gaps", "priority": 100},
                {"source": "web_discovery", "query": "乌托邦整合包 模组列表 整合包介绍", "reason": "public pages", "priority": 90},
            ],
        },
        question,
        ROOT / "data" / "crawler_exports",
        max_tasks=8,
        session_summary={
            "delivery_target": "MCagent/RAG",
            "collection_target": question,
            "original_user_message": question,
            "handoff_brief": "历史经验提示：困难整合包有时可尝试下载页、包体、.mrpack 或 .zip，但这不是当前用户明确要求。",
            "planning_instruction": "先问 MCagent，再补资料；不要把辅助提示当成用户目标。",
        },
    )
    sources = [task["source"] for task in plan["tasks"]]
    assert_true(f"no_archive_from_handoff_brief: {sources}", "modpack_download" not in sources)


if __name__ == "__main__":
    test_direct_crawler_delegate_phrase_is_not_target()
    test_rule_fallback_extracts_domain_target_from_agent_handoff()
    test_rule_fallback_rejects_numbered_action_plan_fragments_as_targets()
    test_rule_fallback_keeps_quoted_slash_alias_modpack_target()
    test_rule_fallback_extracts_modern_utf8_chinese_modpack_handoff()
    test_mcagent_delegation_extracts_modpack_entity_from_full_instruction()
    test_utf8_mcagent_delegation_extracts_clean_named_alias_target()
    test_session_target_rejects_generic_relation_phrase()
    test_session_target_strips_mcagent_reply_handoff_noise()
    test_session_target_extracts_entity_from_mcagent_reported_gap()
    test_gap_collection_fallback_prefers_generic_web_and_bound_queries()
    test_selected_crawler_action_plan_materializes_mcagent_context_first()
    test_gap_summary_handoff_does_not_reinsert_duplicate_mcagent_context()
    test_local_inventory_text_does_not_replace_crawler_mcagent_context_step()
    test_inventory_discovered_gap_phrase_does_not_become_target_prefix()
    test_gap_collection_with_archive_goal_still_asks_mcagent_first()
    test_gap_collection_without_archive_goal_does_not_schedule_modpack_download()
    test_llm_plan_gap_collection_is_rebalanced_to_generic_tools()
    test_sanitized_plan_limits_slow_mcmod_fanout()
    test_general_domain_fallback_does_not_default_to_minecraft_tools()
    test_general_domain_sanitize_filters_minecraft_tool_noise()
    test_gap_collection_rejects_literal_missing_as_web_topic()
    test_reflection_replaces_literal_gap_pending_query()
    test_reflection_llm_failure_continues_existing_pending_task()
    test_archive_guard_does_not_force_download_when_mcagent_context_has_archive_evidence()
    test_reflection_after_mcagent_gap_reply_lets_crawler_llm_choose_public_web()
    test_reflection_explicit_archive_goal_still_uses_crawler_llm_not_guard()
    test_topic_discovery_review_uses_crawler_profile_client()
    test_fallback_plan_confirmation_lets_crawler_pick_existing_task()
    test_fallback_plan_confirmation_failure_stops_before_tools()
    test_fallback_plan_confirmation_empty_json_reports_real_failure()
    test_sanitize_plan_rejects_tasks_that_follow_crawler_word_instead_of_target()
    test_sanitize_plan_drops_local_file_tasks_without_objective_path()
    test_general_chinese_collection_extracts_entity_not_delivery_phrase()
    test_planner_client_uses_bounded_timeout_for_agent_planning()
    test_job_planner_default_startup_timeout_is_bounded()
    test_planner_json_chat_requests_json_object_mode()
    test_sanitize_plan_accepts_llm_priority_labels()
    test_sanitize_plan_normalizes_llm_action_aliases()
    test_gap_question_text_still_keeps_mcagent_context_first()
    test_fallback_confirmation_finish_is_overridden_for_target_bound_pending_task()
    test_rag_structured_evidence_request_does_not_force_browser_collect()
    test_mcagent_reply_gap_handoff_does_not_prioritize_modpack_download()
    test_mcagent_reported_gap_fallback_does_not_force_archive_first()
    test_llm_gap_plan_does_not_inject_modpack_download_without_archive_goal()
    test_pronoun_collection_target_falls_back_to_original_user_entity()
    test_handoff_brief_archive_words_do_not_create_archive_intent()
    test_structured_xlsx_request_uses_browser_collect()
    test_modpack_archive_fallback_does_not_become_browser_collect()
    test_english_modpack_archive_fallback_extracts_pack_name_and_download_query()
    test_english_handoff_extracts_pack_name_from_for_the_minecraft_modpack_phrase()
    test_type_discovery_request_does_not_force_modpack_archive_download()
    test_mcagent_gap_fill_request_does_not_force_archive_download_first()
    test_create_mod_fallback_does_not_inject_unrelated_component_queries()
    test_reflection_allows_url_seen_in_manifest_preview()
    test_job_planner_timeout_returns_executable_fallback()
    print("crawler_planner_timeout_scenarios passed")
