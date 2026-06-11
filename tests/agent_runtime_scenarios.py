from __future__ import annotations

import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.agent_runtime import (  # noqa: E402
    TOOL_RESULT_STATUSES,
    build_handoff_contract,
    classify_crawler_tool_result,
    crawler_collection_catalog_prompt,
    domain_collection_tools_for_crawler,
    general_collection_tools_for_crawler,
    make_agent_loop_event,
    normalize_agent_tool_decision,
    objective_tools_for_agent,
    tool_catalog_prompt,
    tool_catalog_json,
)
from mcagent.crawler_llm_planner import plan_crawler_tasks_rule_fallback  # noqa: E402
from mcagent.web_server import _job_readable_summary  # noqa: E402


def assert_equal(name: str, actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"{name}: expected {expected!r}, got {actual!r}")


def assert_true(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name}: {detail}")


def test_tool_observation_matrix() -> None:
    cases = [
        ("records_pending_review", {"source": "mcmod", "returncode": 0, "manifest_stats": {"records": 2}, "topic_validation": {"matched": True}}),
        ("ok", {"source": "mcmod", "returncode": 0, "manifest_stats": {"records": 2}, "topic_validation": {"matched": True, "crawler_review_action": "accept"}}),
        ("empty", {"source": "mcmod", "returncode": 0, "empty_result": True, "manifest_stats": {"records": 0}}),
        ("off_topic", {"source": "web_discovery", "returncode": 0, "off_topic_result": True, "manifest_stats": {"records": 1}}),
        ("uncertain", {"source": "fetch_url", "returncode": 0, "uncertain_result": True, "manifest_stats": {"records": 1}}),
        ("duplicate_reused", {"source": "mcmod", "returncode": 0, "existing_evidence_reused": {"matched": True}, "manifest_stats": {"records": 0, "skipped": 3}}),
        ("blocked", {"source": "planner", "returncode": 2, "empty_query_result": True}),
        ("stopped", {"source": "browser_collect", "returncode": 130}),
        ("timeout", {"source": "web_discovery", "returncode": 124, "timed_out": True}),
        ("quota_limited", {"source": "playwright", "returncode": 1, "output": "HTTP 429 quota exceeded"}),
        ("captcha_required", {"source": "browser_collect", "returncode": 1, "output": "captcha verification required"}),
        ("login_required", {"source": "browser_collect", "returncode": 1, "output": "please login or sign in"}),
        ("auth_required", {"source": "playwright", "returncode": 1, "output": "HTTP 401 unauthorized"}),
        ("network_error", {"source": "fetch_url", "returncode": 1, "output": "failed to fetch: DNS connection error"}),
        ("parse_error", {"source": "playwright", "returncode": 1, "output": "JSONDecodeError invalid json"}),
        ("execution_error", {"source": "unknown", "returncode": 1, "output": "script failed"}),
        ("records_pending_review", {"source": "playwright", "returncode": 0, "manifest_stats": {"records": 1}, "output": "Project not found. You may have mistyped the project's URL."}),
        ("records_pending_review", {"source": "save_artifact", "returncode": 0, "manifest_stats": {"records": 1, "usable_records": 0, "empty_records": 1, "record_bytes": 0}, "topic_validation": {"matched": True}}),
        ("records_pending_review", {"source": "modrinth", "returncode": 0, "manifest_stats": {"records": 0, "skipped": 3}, "output": "Skipped unchanged: 3"}),
    ]
    for expected, result in cases:
        observation = classify_crawler_tool_result(result)
        assert_true(f"known_status_{expected}", observation.status in TOOL_RESULT_STATUSES)
        assert_equal(f"classify_{expected}", observation.status, expected)
        assert_true(f"summary_{expected}", bool(observation.summary))
        if expected == "records_pending_review":
            assert_true("pending_review_not_ok", observation.bad)


def test_agent_loop_event_keeps_trace_shape() -> None:
    event = make_agent_loop_event("observe", "received", {"question": "你好"})
    trace = event.to_trace_dict()
    assert_equal("trace_stage", trace["stage"], "observe")
    assert_equal("trace_status", trace["status"], "received")
    assert_equal("trace_detail", trace["detail"], {"question": "你好"})
    assert_true("trace_time", isinstance(trace["time"], float) and trace["time"] > 0)


def test_agent_tool_decision_normalization() -> None:
    rag_decision = normalize_agent_tool_decision(
        {"tool": "local_rag_search", "reason": "needs local evidence", "rag_focus": "乌托邦探险之旅玩法"},
        agent_id="mcagent_rag",
        original_question="介绍乌托邦玩法",
        planner="test",
    )
    assert_equal("rag_alias", rag_decision.tool, "answer")
    assert_equal("rag_focus", rag_decision.rag_focus, "乌托邦探险之旅玩法")

    crawler_answer = normalize_agent_tool_decision(
        {"tool": "answer", "reason": "explain crawler capability"},
        agent_id="crawler_agent",
        original_question="你能做什么",
        planner="test",
    )
    assert_equal("crawler_answer_alias", crawler_answer.tool, "direct_answer")

    unknown = normalize_agent_tool_decision(
        {"tool": "made_up_tool", "collection_target": "采集资料"},
        agent_id="mcagent_rag",
        original_question="随便问问",
        planner="test",
    )
    assert_equal("unknown_tool_router_error", unknown.tool, "router_error")

    crawler_extract = normalize_agent_tool_decision(
        {"tool": "temporary_extract", "collection_target": "读取并总结公开网页"},
        agent_id="crawler_agent",
        original_question="总结这个网页，不用保存",
        planner="test",
    )
    assert_equal("crawler_temporary_extract_tool", crawler_extract.tool, "temporary_extract")

    agent_message = normalize_agent_tool_decision(
        {"tool": "agent_message", "to_agent": "CrawlerAgent", "content": "please answer this directly", "intent": "agent_question"},
        agent_id="mcagent_rag",
        original_question="ask CrawlerAgent to answer this directly",
        planner="test",
    )
    assert_equal("mcagent_agent_message_tool", agent_message.tool, "agent_message")
    assert_equal("mcagent_agent_message_to", agent_message.to_agent, "CrawlerAgent")
    assert_equal("mcagent_agent_message_content", agent_message.content, "please answer this directly")

    mcagent_delegate = normalize_agent_tool_decision(
        {"tool": "delegate_crawler", "collection_target": "采集落幕曲新手攻略", "delivery_target": "MCagent/RAG"},
        agent_id="mcagent_rag",
        original_question="让 Crawler 采集落幕曲新手攻略",
        planner="test",
    )
    assert_equal("mcagent_delegate_not_exposed", mcagent_delegate.tool, "router_error")

    legacy_planned = normalize_agent_tool_decision(
        {"tool": "answer_then_crawler", "action_plan": [{"tool": "local_rag_search", "goal": "先查本地"}, {"tool": "delegate_crawler", "goal": "再补缺口"}]},
        agent_id="mcagent_rag",
        original_question="本地有什么，缺什么让 Crawler 去找",
        planner="test",
    )
    assert_equal("legacy_planned_alias_rejected", legacy_planned.tool, "router_error")

    compact_planned = normalize_agent_tool_decision(
        {
            "tool": "planned",
            "action_plan": [
                {"step": 1, "tool": "mcagent_context", "goal": "ask MCagent for local context"},
                {"step": 2, "tool": "delegate_crawler", "goal": "collect missing evidence"},
            ],
        },
        agent_id="crawler_agent",
        original_question="Before collecting, ask MCagent for local context, then collect.",
        planner="test",
    )
    assert_equal("compact_planned_alias", compact_planned.tool, "planned_workflow")

    vague_delegate = normalize_agent_tool_decision(
        {"tool": "crawler", "collection_target": "采集公开资料"},
        agent_id="mcagent_rag",
        original_question="让 Crawler 去采集公开资料",
        planner="test",
    )
    assert_equal("vague_crawler_alias_rejected", vague_delegate.tool, "router_error")

    vague_inventory = normalize_agent_tool_decision(
        {"tool": "inventory", "reason": "inspect coverage"},
        agent_id="mcagent_rag",
        original_question="本地有哪些资料",
        planner="test",
    )
    assert_equal("vague_inventory_alias_rejected", vague_inventory.tool, "router_error")

    labelled_step = normalize_agent_tool_decision(
        {
            "tool": "planned_workflow",
            "action_plan": [
                {"step": "check_coverage", "tool": "local_corpus_inventory", "goal": "先盘点覆盖范围"},
                {"step": "fill_gaps", "tool": "delegate_crawler", "goal": "再交给 Crawler 补缺口"},
            ],
        },
        agent_id="mcagent_rag",
        original_question="本地缺什么，让 Crawler 补",
        planner="test",
    )
    assert_equal("labelled_step_number", labelled_step.action_plan[0]["step"], 1)
    assert_equal("labelled_step_label", labelled_step.action_plan[0]["step_label"], "check_coverage")
    assert_equal("second_labelled_step_number", labelled_step.action_plan[1]["step"], 2)


def test_handoff_contract_preserves_context() -> None:
    contract = build_handoff_contract(
        requested_by="user_via_mcagent",
        from_agent="MCagent",
        to_agent="CrawlerAgent",
        user_request="现在乌托邦整合包本地缺哪些资料，列出来，然后让 Crawler 去补充",
        task_goal="补充乌托邦探险之旅的模组列表、任务线、玩法和版本差异",
        delivery_target="MCagent/RAG",
        known_context="MCagent 已总结现有资料缺少完整模组列表和任务线。",
        acceptance_criteria=["保留原始 URL", "保存 markdown 和 raw HTML", "说明失败原因"],
    )
    text = contract.to_prompt_text()
    assert_true("contract_original_request", "现在乌托邦整合包" in text)
    assert_true("contract_goal", "补充乌托邦探险之旅" in text)
    assert_true("contract_delivery", "MCagent/RAG" in text)
    assert_true("contract_acceptance", "保存 markdown" in text)


def test_tool_catalog_exposes_agent_capabilities() -> None:
    mcagent_catalog = tool_catalog_prompt("mcagent_rag")
    crawler_catalog = crawler_collection_catalog_prompt()
    assert_true("mcagent_direct_answer", "direct_answer" in mcagent_catalog)
    assert_true("mcagent_local_rag", "local_rag_search" in mcagent_catalog)
    assert_true("mcagent_agent_message", "agent_message" in mcagent_catalog and "no-persistence From-Content-To" in mcagent_catalog)
    assert_true("mcagent_no_ask_crawler_agent", "ask_crawler_agent" not in mcagent_catalog)
    assert_true("mcagent_no_delegate_crawler", "delegate_crawler" not in mcagent_catalog, mcagent_catalog)
    assert_true("mcagent_only_message_to_crawler", "MCagent has no separate crawler delegation tool" in mcagent_catalog, mcagent_catalog)
    crawler_route_catalog = tool_catalog_prompt("crawler_agent")
    assert_true("crawler_agent_message", "agent_message" in crawler_route_catalog and "no-persistence From-Content-To" in crawler_route_catalog)
    assert_true("crawler_no_ask_crawler_agent", "ask_crawler_agent" not in crawler_route_catalog)
    assert_true("crawler_mcagent_context", "mcagent_context" in crawler_route_catalog)
    assert_true("crawler_planned_workflow", "planned_workflow" in crawler_route_catalog)
    assert_true("crawler_browser", "browser_collect" in crawler_catalog)
    assert_true("crawler_save_artifact", "save_artifact" in crawler_catalog)
    assert_true("crawler_artifact_ref_schema", "content_ref" in crawler_catalog or "artifact_ref" in crawler_catalog)
    assert_true("crawler_modpack_internal", "modpack_internal" in crawler_catalog)
    assert_true("crawler_research_method", "source graph" in crawler_catalog and "broad keyword blasting" in crawler_catalog)
    assert_true("crawler_source_nodes", "dependency/relation pages" in crawler_catalog and "changelogs/releases" in crawler_catalog)
    assert_true("crawler_modpack_route_order", "Modrinth API" in crawler_catalog and "CurseForge" in crawler_catalog and "GitHub Releases" in crawler_catalog and "packwiz" in crawler_catalog)
    assert_true("crawler_modpack_llm_judgment", "inspect objective evidence before choosing" in crawler_catalog and "Only schedule modpack_internal after a real local archive path" in crawler_catalog)
    assert_true("llm_ownership", "LLM owns interpretation" in mcagent_catalog)
    assert_true("mcagent_identity", "Minecraft-focused knowledge agent" in mcagent_catalog)
    assert_true("mcagent_local_kb", "local Minecraft knowledge base" in mcagent_catalog)
    assert_true("mcagent_inventory_boundary", "entire indexed local Minecraft knowledge base" in mcagent_catalog, mcagent_catalog)
    assert_true("mcagent_rag_not_inventory", "must not be used to claim what the entire local library contains" in mcagent_catalog, mcagent_catalog)


def test_objective_observation_tools_are_role_separated() -> None:
    mcagent_objective = {tool.name for tool in objective_tools_for_agent("mcagent_rag")}
    crawler_objective = {tool.name for tool in objective_tools_for_agent("crawler_agent")}
    assert_true("mcagent_has_local_index_tools", {"read_session_memory", "search_local_index", "inspect_local_corpus", "read_indexed_document"}.issubset(mcagent_objective))
    assert_true("mcagent_no_browser_or_write_tools", not {"web_discovery", "playwright_snapshot", "browser_collect", "save_artifact", "list_directory"}.intersection(mcagent_objective), str(mcagent_objective))
    assert_true("crawler_has_generic_file_tools", {"list_directory", "read_file", "search_files_by_name", "search_files_by_content", "get_file_metadata"}.issubset(crawler_objective))
    assert_true("crawler_has_doc_web_browser_tools", {"extract_text_from_pdf", "extract_text_from_docx", "extract_text_from_excel", "fetch_url", "web_discovery", "playwright_snapshot", "browser_collect", "save_artifact"}.issubset(crawler_objective))
    assert_true("crawler_no_mcagent_index_tool", "inspect_local_corpus" not in crawler_objective)
    mcagent_json = json.loads(tool_catalog_json("mcagent_rag"))
    crawler_json = json.loads(tool_catalog_json("crawler_agent"))
    assert_true("catalog_json_route_tools", any(item["name"] == "agent_message" for item in mcagent_json["route_tools"]))
    assert_true("catalog_json_objective_tools", any(item["name"] == "inspect_local_corpus" for item in mcagent_json["objective_observation_tools"]))
    assert_true("crawler_catalog_json_objective_tools", any(item["name"] == "list_directory" for item in crawler_json["objective_observation_tools"]))


def test_session_summary_preserves_recent_turns_from_ui_history() -> None:
    from mcagent import web_server

    payload = {
        "session_id": "ui-history-context",
        "history": [
            {"role": "user", "text": "问下crawler 1+1=几", "time": 1000},
            {"role": "assistant", "text": "1+1=2，不用 crawler。", "time": 1001},
        ],
    }
    summary = web_server._session_summary(payload)
    turns = summary.get("recent_turns") or []
    assert_true("recent_turns_present", bool(turns), str(summary))
    assert_equal("last_user_question", summary.get("last_user_question"), "问下crawler 1+1=几")
    assert_true("last_assistant_answer", "不用 crawler" in str(summary.get("last_assistant_answer") or ""), str(summary))


def test_session_store_persists_history_summary_and_events() -> None:
    import tempfile
    from pathlib import Path
    from mcagent.session_state import InMemorySessionStore

    with tempfile.TemporaryDirectory() as tmp:
        store = InMemorySessionStore(Path(tmp))
        store.append_turn("persist-demo", {"question": "问下crawler 1+1=几", "answer": "不用 crawler"})
        store.update_summary("persist-demo", lambda current: {**current, "last_user_question": "问下crawler 1+1=几"})
        store.append_event("persist-demo", {"kind": "agent_message", "from_agent": "MCagent", "to_agent": "CrawlerAgent", "content": "1+1=几"})

        reloaded = InMemorySessionStore(Path(tmp))
        assert_equal("persisted_history", reloaded.history("persist-demo")[0]["question"], "问下crawler 1+1=几")
        assert_equal("persisted_summary", reloaded.summary("persist-demo")["last_user_question"], "问下crawler 1+1=几")
        assert_equal("persisted_event", reloaded.events("persist-demo")[0]["to_agent"], "CrawlerAgent")


def test_crawler_collection_tools_are_grouped_by_general_and_domain() -> None:
    general_names = {tool.name for tool in general_collection_tools_for_crawler()}
    minecraft_names = {tool.name for tool in domain_collection_tools_for_crawler("minecraft")}
    assert_true("general_has_web_discovery", "web_discovery" in general_names)
    assert_true("general_has_fetch_url", "fetch_url" in general_names)
    assert_true("general_has_local_files", {"read_local_file", "search_local_files"}.issubset(general_names))
    assert_true("general_has_browser", {"playwright", "browser_collect"}.issubset(general_names))
    assert_true("general_has_artifact", "save_artifact" in general_names)
    assert_true("general_excludes_mcmod", "mcmod" not in general_names)
    assert_true("general_excludes_modrinth", "modrinth" not in general_names)
    assert_true("general_excludes_modpack_download", "modpack_download" not in general_names)
    assert_true("general_excludes_modpack_internal", "modpack_internal" not in general_names)
    assert_true("minecraft_has_domain_tools", {"mcmod", "modrinth", "modpack_download", "modpack_internal"}.issubset(minecraft_names))


def test_job_readable_summary_surfaces_observations() -> None:
    job = {
        "title": "Crawler 多源补库 -> RAG",
        "status": "running",
        "summary": "running",
        "result": {
            "plan": {
                "topic": "乌托邦探险之旅",
                "delivery_target": "MCagent/RAG",
                "coverage_goals": ["完整模组列表", "任务线", "玩法机制"],
                "strategy": "target_fallback_after_llm_planner_error",
                "planner_error": "unit planner failure",
            },
            "planned_tasks": [
                {"source": "mcmod", "query": "乌托邦探险之旅", "reason": "项目页"},
                {"source": "playwright", "query": "乌托邦探险之旅 玩法", "reason": "教程页"},
            ],
            "tasks": [
                {
                    "source": "mcmod",
                    "query": "乌托邦探险之旅",
                    "returncode": 0,
                    "manifest_stats": {"records": 2, "skipped": 0, "errors": 0},
                    "observation": {"status": "ok", "summary": "Tool produced usable records."},
                    "agent_message_exchange": {
                        "request": {"from_agent": "CrawlerAgent", "to_agent": "MCagent", "content": "ask gaps", "intent": "mcagent_context_request"},
                        "reply": {"from_agent": "MCagent", "to_agent": "CrawlerAgent", "content": "gap reply", "intent": "mcagent_context_reply"},
                    },
                },
                {
                    "source": "playwright",
                    "query": "乌托邦探险之旅 玩法",
                    "returncode": 1,
                    "output": "HTTP 429 quota exceeded",
                    "manifest_stats": {"records": 0, "skipped": 0, "errors": 1},
                    "observation": {"status": "quota_limited", "summary": "HTTP 429 quota exceeded"},
                },
            ],
        },
    }
    readable = _job_readable_summary(job)
    assert_equal("readable_target", readable["target"], "乌托邦探险之旅")
    assert_equal("readable_delivery", readable["delivery_target"], "MCagent/RAG")
    assert_equal("ok_count", readable["observation_statuses"].get("ok"), 1)
    assert_equal("quota_count", readable["observation_statuses"].get("quota_limited"), 1)
    assert_equal("latest_status", readable["latest_observation"].get("status"), "quota_limited")
    assert_true("fallback_visible", readable["fallback_used"] and "unit planner failure" in readable["planner_warning"])
    assert_equal("inter_agent_visible", len(readable["inter_agent_messages"]), 2)
    assert_true("plain_summary", bool(readable["plain_summary"]))
    assert_equal("useful_outputs", len(readable["useful_outputs"]), 1)
    assert_true("blocked_outputs", len(readable["blocked_outputs"]) >= 1)


def test_job_readable_recovers_target_from_long_question() -> None:
    job = {
        "title": "Crawler 多源补库 -> RAG",
        "status": "running",
        "result": {
            "plan": {
                "topic": "具体名称与功能简介）",
                "target_hint": "具体名称与功能简介）",
                "question": "请针对Minecraft整合包“乌托邦探险之旅（Utopian Journey）”进行信息采集。目前缺少模组列表。",
                "delivery_target": "MCagent/RAG",
            },
            "planned_tasks": [{"source": "web_discovery", "query": "乌托邦探险之旅 模组列表"}],
            "tasks": [],
        },
    }
    readable = _job_readable_summary(job)
    assert_equal("recovered_target", readable["target"], "乌托邦探险之旅（Utopian Journey）")
    assert_true("headline_uses_recovered_target", "乌托邦探险之旅" in readable["headline"])


def test_crawler_delegation_requires_explicit_agent_route() -> None:
    source = (ROOT / "mcagent" / "web_server.py").read_text(encoding="utf-8")
    router_source = (ROOT / "mcagent" / "agent_router.py").read_text(encoding="utf-8")
    combined_source = source + "\n" + router_source
    doc = (ROOT / "docs" / "agent_development_guide.md").read_text(encoding="utf-8")
    assert_true("no_answer_post_scan_helper", "_answer_requires_auto_delegate" not in source)
    assert_true("no_answer_gap_regex_helper", "_answer_indicates_missing_data" not in source)
    assert_true("no_post_answer_delegate_trace", "answer_marked_missing" not in source)
    assert_true("router_error_no_delegate_fallback", 'fallback_tool = "delegate_crawler"' not in source)
    assert_true("router_error_no_answer_fallback", 'fallback_tool = "answer"' not in source)
    assert_true("router_error_route_exists", '"tool": "router_error"' in combined_source and 'route_intent == "router_error"' in combined_source)
    assert_true("no_dead_keyword_status_router", "def _is_crawler_status_request" not in source)
    assert_true("no_dead_keyword_start_router", "def _is_crawler_start_request" not in source)
    assert_true("no_dead_keyword_route_intent", "def _mcagent_route_intent" not in source)
    assert_true("router_prompt_moved", "def _agent_tool_decision" not in source and "def _agent_confirm_next_step" not in source)
    assert_true("web_no_router_prompt", "你是当前对话里的 Agent 工具选择器" not in source and "你是当前 Agent 的下一步行动确认器" not in source)
    assert_true("no_answer_prompt_auto_delegate", "或本地证据不足时，应使用这个能力" not in source)
    assert_true("no_doc_auto_delegate_gap", "发现证据不足时，说明缺口，并把资料缺口交给 CrawlerAgent" not in doc)
    assert_true("no_doc_final_answer_auto_delegate", "若最终回答中 LLM 判断证据不足，才把缺口交给 CrawlerAgent" not in doc)
    assert_true("planned_delegate_branch_exists", "if planned_delegate:" in source)
    assert_true("insufficient_evidence_no_auto_delegate", '"delegated": False' in source)
    assert_true("status_reports_latest_crawler_job", "最近 Crawler 任务" in source and "补到/复用" in source and "受限/低价值" in source)
    assert_true("jobs_persist_across_restart", "JOBS_HISTORY_PATH" in source and "_persist_jobs_locked" in source and "_restore_jobs_locked" in source)
    assert_true("mcagent_semantic_identity_prompt", "Minecraft 资料 Agent" in router_source and "语义判断" in router_source and "不是通用关键词路由器" in router_source)
    planner_source = (ROOT / "mcagent" / "crawler_llm_planner.py").read_text(encoding="utf-8")
    assert_true("crawler_method_prompt", "Research method: avoid broad keyword blasting" in planner_source and "build a source graph" in planner_source)
    assert_true("crawler_pressure_replan_prompt", "When collection pressure rises, replan by source graph" in planner_source)
    assert_true("crawler_archive_method_prompt", "versions and files.url" in planner_source and "browser_download_url" in planner_source and "pack.toml/index.toml" in planner_source)
    assert_true("crawler_cloud_drive_blocker_prompt", "Quark" in planner_source and "direct public .mrpack/.zip URL" in planner_source)


def test_crawler_handoff_target_overrides_old_session_topic() -> None:
    new_target = "Collect complete Minecraft modpack data for XYZABC, including mod list and gameplay guide."
    stale_topic = "Introduce Utopian Journey modpack"
    summary = {
        "current_topic": stale_topic,
        "topics": [stale_topic],
        "collection_target": new_target,
        "task_goal": new_target,
        "authoritative_task_goal": new_target,
        "delivery_target": "MCagent/RAG",
        "known_context": "Previous turn discussed Utopian Journey, but this handoff explicitly asks for a new target.",
    }
    plan = plan_crawler_tasks_rule_fallback(
        new_target,
        ROOT / "data" / "crawler_exports",
        max_tasks=8,
        planner_error="unit test",
        session_summary=summary,
    )
    queries = " ".join(str(task.get("query") or "") for task in plan.get("tasks", []))
    assert_true("new_target_topic", "XYZABC" in str(plan.get("topic") or ""))
    assert_true("new_target_queries", "XYZABC" in queries)
    assert_true("stale_topic_not_authoritative", not str(plan.get("topic") or "").startswith("Introduce Utopian Journey"))


def main() -> int:
    test_tool_observation_matrix()
    test_agent_loop_event_keeps_trace_shape()
    test_agent_tool_decision_normalization()
    test_handoff_contract_preserves_context()
    test_tool_catalog_exposes_agent_capabilities()
    test_objective_observation_tools_are_role_separated()
    test_crawler_collection_tools_are_grouped_by_general_and_domain()
    test_job_readable_summary_surfaces_observations()
    test_job_readable_recovers_target_from_long_question()
    test_crawler_delegation_requires_explicit_agent_route()
    test_crawler_handoff_target_overrides_old_session_topic()
    print("AGENT RUNTIME SCENARIOS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
