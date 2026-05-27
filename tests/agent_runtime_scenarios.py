from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.agent_runtime import (  # noqa: E402
    TOOL_RESULT_STATUSES,
    build_handoff_contract,
    classify_crawler_tool_result,
    crawler_collection_catalog_prompt,
    make_agent_loop_event,
    normalize_agent_tool_decision,
    tool_catalog_prompt,
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
        ("ok", {"source": "mcmod", "returncode": 0, "manifest_stats": {"records": 2}}),
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
    ]
    for expected, result in cases:
        observation = classify_crawler_tool_result(result)
        assert_true(f"known_status_{expected}", observation.status in TOOL_RESULT_STATUSES)
        assert_equal(f"classify_{expected}", observation.status, expected)
        assert_true(f"summary_{expected}", bool(observation.summary))


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

    planned = normalize_agent_tool_decision(
        {"tool": "answer_then_crawler", "action_plan": [{"tool": "local_rag_search", "goal": "先查本地"}, {"tool": "delegate_crawler", "goal": "再补缺口"}]},
        agent_id="mcagent_rag",
        original_question="本地有什么，缺什么让 Crawler 去找",
        planner="test",
    )
    assert_equal("planned_alias", planned.tool, "planned_workflow")
    assert_equal("planned_steps", len(planned.action_plan), 2)


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
    crawler_route_catalog = tool_catalog_prompt("crawler_agent")
    assert_true("crawler_mcagent_context", "mcagent_context" in crawler_route_catalog)
    assert_true("crawler_browser", "browser_collect" in crawler_catalog)
    assert_true("crawler_save_artifact", "save_artifact" in crawler_catalog)
    assert_true("crawler_artifact_ref_schema", "content_ref" in crawler_catalog or "artifact_ref" in crawler_catalog)
    assert_true("crawler_modpack_internal", "modpack_internal" in crawler_catalog)
    assert_true("crawler_research_method", "source graph" in crawler_catalog and "broad keyword blasting" in crawler_catalog)
    assert_true("crawler_source_nodes", "dependency/relation pages" in crawler_catalog and "changelogs/releases" in crawler_catalog)
    assert_true("llm_ownership", "LLM owns interpretation" in mcagent_catalog)
    assert_true("mcagent_identity", "Minecraft-focused knowledge agent" in mcagent_catalog)
    assert_true("mcagent_local_kb", "local Minecraft knowledge base" in mcagent_catalog)


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


def test_crawler_handoff_target_overrides_old_session_topic() -> None:
    new_target = "请收集关于 Minecraft 整合包 XYZABC 的详细资料，包括模组列表和玩法指南"
    stale_topic = "介绍一下乌托邦整合包"
    summary = {
        "current_topic": stale_topic,
        "topics": [stale_topic],
        "collection_target": new_target,
        "task_goal": new_target,
        "authoritative_task_goal": new_target,
        "delivery_target": "MCagent/RAG",
        "known_context": "上一轮用户在聊乌托邦，但这一轮已经明确委托新的采集目标。",
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
    assert_true("stale_topic_not_authoritative", not str(plan.get("topic") or "").startswith("介绍一下乌托邦"))


def main() -> int:
    test_tool_observation_matrix()
    test_agent_loop_event_keeps_trace_shape()
    test_agent_tool_decision_normalization()
    test_handoff_contract_preserves_context()
    test_tool_catalog_exposes_agent_capabilities()
    test_job_readable_summary_surfaces_observations()
    test_job_readable_recovers_target_from_long_question()
    test_crawler_delegation_requires_explicit_agent_route()
    test_crawler_handoff_target_overrides_old_session_topic()
    print("AGENT RUNTIME SCENARIOS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

