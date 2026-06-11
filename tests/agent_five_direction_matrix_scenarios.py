from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def assert_true(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name}: {detail}")


def test_matrix_document_covers_five_directions_with_examples() -> None:
    doc = (ROOT / "docs" / "agent_test_matrix.md").read_text(encoding="utf-8")
    headings = [
        "方向一：MCagent 回答本地已有资料",
        "方向二：MCagent 发现本地资料不足并委托 Crawler",
        "方向三：用户直连 Crawler 获取指定网页数据但不保存",
        "方向四：Crawler 为 MCagent/RAG 找资料并保存入库",
        "方向五：Crawler 获取网页/数据并保存到用户指定本地位置",
    ]
    for heading in headings:
        assert_true(f"heading_{heading}", heading in doc)
    assert_true("matrix_has_many_examples", doc.count("- “") >= 20)
    assert_true("matrix_rejects_hardcoded_rules", "测试例子不是硬编码规则" in doc)


def test_frontend_does_not_show_fixed_three_way_prompt() -> None:
    app = (ROOT / "frontend" / "static" / "app.js").read_text(encoding="utf-8")
    forbidden = [
        "判断这是问答、状态，还是要交给 Crawler 采集",
        "判断这份数据是给你看，还是要清洗成 MCagent/RAG 能用的格式",
    ]
    for phrase in forbidden:
        assert_true(f"forbidden_frontend_phrase_{phrase}", phrase not in app)
    assert_true("no_fixed_mcagent_status", "MCagent 正在读取你的问题。" not in app)
    assert_true("no_fixed_crawler_status", "CrawlerAgent 正在读取你的任务。" not in app)
    assert_true("first_person_initial_status", "我收到你的问题" in app and "我收到你的 Crawler 请求" in app)
    assert_true("right_action_timeline", "agentActionTimeline" in app and "recordAgentAction" in app)


def test_frontend_uses_compact_job_card_instead_of_default_trace_noise() -> None:
    app = (ROOT / "frontend" / "static" / "app.js").read_text(encoding="utf-8")
    assert_true("compact_job_renderer", "function renderUsefulOutputs" in app)
    assert_true("compact_summary", "plain_summary" in app)
    assert_true("trace_not_rendered_by_default", "renderTrace(message.trace" not in app)
    assert_true("inter_agent_folded", "Agent 间通信" in app and "renderInterAgentMessages" in app)
    assert_true("job_details_open_by_default", "查看采集详情" in app and 'detailsAttrs(key || "job", true)' in app)
    assert_true("job_relink_after_reload", "function relinkTrackedJobs" in app and "message?.jobId" in app)
    assert_true("main_progress_mentions_current_step", "当前进度：" in app and "第 ${current || 0}/${total} 步" in app)
    assert_true("right_panel_folds_agent_judgement", "<summary>展开技术细节</summary>" in app)
    assert_true("job_actor_panel_visible", "function renderJobActorPanel" in app and "job-actor-panel" in app)
    assert_true("job_actor_panel_names_subject", "CrawlerAgent" in app and "当前动作" in app and "成果" in app)
    assert_true("crawler_polling_appends_process", "appendProcessStep(message, crawlerProgressText(job)" in app)
    assert_true("crawler_polling_does_not_replace_answer", "message.text = crawlerProgressText(job)" not in app)
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert_true("right_panel_primary_agent_actions", "Agent 动作" in html and "agentActionTimeline" in html)


def test_frontend_keeps_streamed_answer_when_connection_ends_badly() -> None:
    app = (ROOT / "frontend" / "static" / "app.js").read_text(encoding="utf-8")
    tail_buffer_block = app[app.index("if (buffer.trim())") : app.index("return finalResponse;")]
    guarded_error_block = app[app.index("hasUsableAnswer") : app.index("} finally {", app.index("hasUsableAnswer"))]
    assert_true("tail_response_calls_handler", "handlers.onResponse?.(event.data);" in tail_buffer_block)
    assert_true("stream_fallback_final_text", "agentReplyContent(data) || streamedAnswer" in app)
    assert_true("usable_answer_guard", "hasUsableAnswer" in guarded_error_block)
    assert_true("partial_answer_not_replaced_by_error", "message.finalAnswerText = streamedAnswer || message.finalAnswerText ||" in guarded_error_block)
    assert_true("final_answer_not_polluted_by_process_block", 'return `${processText}' not in app and '最终回答：' not in app)
    assert_true("process_log_recorded_for_right_panel", "function appendProcessStep" in app and "recordAgentAction" in app)
    assert_true("chat_window_does_not_duplicate_process_panel", "renderProcessLog(message.processLog" not in app)
    assert_true("initial_step_enters_process_log", "function setInitialProcessStep" in app and "setInitialProcessStep(pendingMessage, initialText" in app)
    assert_true("process_log_not_truncated", "message.processLog = [...current, value];" in app)
    assert_true("sources_cleared_only_without_usable_answer", "renderSources([]);" in guarded_error_block.split("} else {", 1)[1])


def test_frontend_crawler_panel_uses_agent_message_endpoint() -> None:
    app = (ROOT / "frontend" / "static" / "app.js").read_text(encoding="utf-8")
    start = app.index("async function runCrawler()")
    end = app.index("\nfunction initEvents()", start)
    body = app[start:end]
    assert_true("crawler_panel_uses_message_endpoint", 'api("/api/agent-message"' in body)
    assert_true("crawler_panel_from_agent", 'from_agent: "User"' in body)
    assert_true("crawler_panel_to_agent", 'to_agent: "CrawlerAgent"' in body)
    assert_true("crawler_panel_content", "content: question" in body)
    assert_true("crawler_panel_no_legacy_start_endpoint", "/api/jobs/start-crawler" not in app)
    assert_true("crawler_panel_does_not_preselect_delegate_tool", 'tool: "delegate_crawler"' not in body)
    assert_true("crawler_panel_adds_chat_message", 'addMessage("user", question)' in body and 'addMessage("assistant", "处理中...", "CrawlerAgent")' in body)
    assert_true("crawler_panel_tracks_job_on_message", "rememberJobMessage(data.job, session.id, pendingIndex)" in body)
    assert_true("crawler_panel_appends_process_log", "appendProcessStep(message, crawlerProgressText(data.job)" in body)
    assert_true("crawler_panel_no_system_task_ticket", "CrawlerAgent 采集任务已启动：" not in body)


def test_backend_answer_templates_are_not_task_tickets() -> None:
    web_server = (ROOT / "mcagent" / "web_server.py").read_text(encoding="utf-8")
    note_start = web_server.index("def _crawler_delegation_note_for")
    note_end = web_server.index("\ndef _crawler_agent_context_delegation_answer", note_start)
    note_body = web_server[note_start:note_end]
    forbidden = [
        "补库动作：",
        "任务ID：",
        "进度、当前动作、成果、自审",
        "任务卡片",
    ]
    for phrase in forbidden:
        assert_true(f"no_task_ticket_phrase_{phrase}", phrase not in note_body)
    delegate_start = web_server.index("def _handle_delegate_crawler_route")
    delegate_end = web_server.index("\ndef _mcagent_delegate_route_as_agent_message", delegate_start)
    delegate_body = web_server[delegate_start:delegate_end]
    assert_true("agent_message_summary_first_person_source", "我已通过 From-Content-To 消息询问" in web_server)
    assert_true("no_agent_message_third_person_summary_source", "CrawlerAgent 的回答如下" not in web_server)
    assert_true("crawler_delegate_first_person_source", "我是 CrawlerAgent。我已经收到这条 AgentMessage" in delegate_body)
    assert_true("crawler_delegate_no_third_person_job_source", "Crawler 多源采集任务已启动" not in delegate_body)
    assert_true("crawler_delegate_no_task_id_answer_source", "任务ID：" not in delegate_body and "任务卡片" not in delegate_body)
    assert_true("planned_workflow_plan_text_not_prepended", 'answer = plan_text + "\\n\\n" + answer' not in web_server)


def test_crawler_tool_catalog_exposes_temporary_and_persistent_paths() -> None:
    runtime = (ROOT / "mcagent" / "agent_runtime.py").read_text(encoding="utf-8")
    assert_true("temporary_extract_tool", 'name="temporary_extract"' in runtime)
    assert_true("persistent_delegate_tool", 'name="delegate_crawler"' in runtime)
    assert_true("browser_collect_tool", 'name="browser_collect"' in runtime)
    assert_true("save_artifact_tool", 'name="save_artifact"' in runtime)
    assert_true("no_persistence_side_effect", "network_only_no_filesystem_persistence" in runtime)
    assert_true("persistent_side_effect", "start_background_job" in runtime)


def test_router_prompt_does_not_hardcode_url_no_save_rule() -> None:
    router = (ROOT / "mcagent" / "agent_router.py").read_text(encoding="utf-8")
    forbidden = [
        "给出一个或多个具体公开 URL",
        "明确不保存、不入库、不交给 MCagent/RAG，则选择 temporary_extract",
    ]
    for phrase in forbidden:
        assert_true(f"no_hardcoded_router_rule_{phrase}", phrase not in router)
    assert_true("tool_catalog_still_used", "tool_catalog_prompt(agent)" in router)


def main() -> int:
    test_matrix_document_covers_five_directions_with_examples()
    test_frontend_does_not_show_fixed_three_way_prompt()
    test_frontend_uses_compact_job_card_instead_of_default_trace_noise()
    test_frontend_keeps_streamed_answer_when_connection_ends_badly()
    test_frontend_crawler_panel_uses_agent_message_endpoint()
    test_backend_answer_templates_are_not_task_tickets()
    test_crawler_tool_catalog_exposes_temporary_and_persistent_paths()
    test_router_prompt_does_not_hardcode_url_no_save_rule()
    print("agent_five_direction_matrix_scenarios passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
