from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.agent_execution import build_agent_execution_context  # noqa: E402
from mcagent.agent_router import AgentToolRouterService, LlmAgentToolRouterService, json_object_from_llm_text  # noqa: E402
from mcagent.config import AppConfig, ChunkingConfig, EmbeddingConfig, OllamaConfig, PathsConfig, RetrievalConfig  # noqa: E402


def make_temp_config(root: Path) -> AppConfig:
    data = root / "data"
    source = data / "crawler_exports"
    source.mkdir(parents=True, exist_ok=True)
    return AppConfig(
        paths=PathsConfig(
            project_root=root,
            source_dir=source,
            db_path=data / "mcagent.sqlite",
            index_path=data / "vector_index.npz",
        ),
        embedding=EmbeddingConfig(),
        chunking=ChunkingConfig(),
        retrieval=RetrievalConfig(),
        ollama=OllamaConfig(model="router-test-model"),
    )


def assert_equal(name: str, actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"{name}: expected {expected!r}, got {actual!r}")


def assert_true(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name}: {detail}")


def make_run(question: str = "介绍乌托邦"):
    tmp = tempfile.TemporaryDirectory()
    config = make_temp_config(Path(tmp.name))
    run = build_agent_execution_context(config, {"question": question, "agent": "mcagent_rag", "model": "router-model"}, token_resolver=lambda *_args: 1200)
    return tmp, run


def make_crawler_collection_run(question: str = "采集公开资料"):
    tmp = tempfile.TemporaryDirectory()
    config = make_temp_config(Path(tmp.name))
    payload = {
        "question": question,
        "agent": "crawler_agent",
        "model": "router-model",
        "agent_message": {
            "from_agent": "MCagent",
            "from_agent_id": "mcagent_rag",
            "to_agent": "CrawlerAgent",
            "to_agent_id": "crawler_agent",
            "content": question,
            "intent": "collection_request",
            "metadata": {"tool": "delegate_crawler", "delivery_target": "MCagent/RAG"},
        },
    }
    run = build_agent_execution_context(config, payload, token_resolver=lambda *_args: 1200)
    return tmp, run


def test_router_records_decision_and_confirmation() -> None:
    tmp, run = make_run()
    try:
        service = AgentToolRouterService(
            decide_tool=lambda *args, **kwargs: {"tool": "answer", "reason": "use local evidence", "rag_focus": "乌托邦玩法"},
            confirm_next_step=lambda *args, **kwargs: {"proceed": True, "tool": kwargs["proposed_tool"], "goal": "ok", "reason": "confirmed"},
            action_plan_has_tool=lambda _plan, _tool: False,
        )
        route = service.route(run, session_summary={"topics": ["乌托邦"]})
    finally:
        tmp.cleanup()

    assert_equal("route_intent", route.route_intent, "answer")
    assert_equal("rag_focus", route.rag_focus, "乌托邦玩法")
    statuses = [(step["stage"], step["status"]) for step in run.trace.steps]
    assert_true("tool_selected_trace", ("decide", "tool_selected") in statuses)
    assert_true("confirmation_trace", ("decide", "next_step_confirmed") in statuses)
    assert_true("rag_focus_trace", ("plan", "rag_focus") in statuses)


def test_router_respects_agent_suggested_tool() -> None:
    tmp, run = make_run("你好")
    try:
        service = AgentToolRouterService(
            decide_tool=lambda *args, **kwargs: {"tool": "answer", "reason": "initial"},
            confirm_next_step=lambda *args, **kwargs: {"proceed": False, "tool": "answer", "suggested_tool": "direct_answer", "goal": "greet", "reason": "no retrieval"},
            action_plan_has_tool=lambda _plan, _tool: False,
        )
        route = service.route(run, session_summary={})
    finally:
        tmp.cleanup()

    assert_equal("suggested_route", route.route_intent, "direct_answer")


def test_router_error_stops_before_confirmation() -> None:
    tmp, run = make_run("介绍一个网页")
    calls = {"confirm": 0}
    try:
        service = AgentToolRouterService(
            decide_tool=lambda *args, **kwargs: {"tool": "router_error", "reason": "invalid tool"},
            confirm_next_step=lambda *args, **kwargs: calls.__setitem__("confirm", calls["confirm"] + 1) or {"proceed": True},
            action_plan_has_tool=lambda _plan, _tool: False,
        )
        route = service.route(run, session_summary={})
    finally:
        tmp.cleanup()

    assert_equal("router_error_route", route.route_intent, "router_error")
    assert_equal("confirm_not_called", calls["confirm"], 0)
    assert_equal("router_error_proceed", route.route_confirmation["proceed"], False)


def test_no_persistence_routes_skip_second_llm_confirmation() -> None:
    tmp, run = make_run("Temporarily read one URL and do not save anything.")
    calls = {"confirm": 0}
    try:
        service = AgentToolRouterService(
            decide_tool=lambda *args, **kwargs: {"tool": "temporary_extract", "reason": "read only without persistence"},
            confirm_next_step=lambda *args, **kwargs: calls.__setitem__("confirm", calls["confirm"] + 1) or {"proceed": True},
            action_plan_has_tool=lambda _plan, _tool: False,
        )
        route = service.route(run, session_summary={})
    finally:
        tmp.cleanup()

    assert_equal("route", route.route_intent, "temporary_extract")
    assert_equal("confirm_not_called", calls["confirm"], 0)
    assert_equal("confirmation_planner", route.route_confirmation["planner"], "runtime")

    tmp2, run2 = make_run("Ask CrawlerAgent for its own simple reply.")
    calls = {"confirm": 0}
    try:
        service = AgentToolRouterService(
            decide_tool=lambda *args, **kwargs: {"tool": "agent_message", "reason": "route a normal message", "to_agent": "CrawlerAgent", "content": "simple reply"},
            confirm_next_step=lambda *args, **kwargs: calls.__setitem__("confirm", calls["confirm"] + 1) or {"proceed": True},
            action_plan_has_tool=lambda _plan, _tool: False,
        )
        message_route = service.route(run2, session_summary={})
    finally:
        tmp2.cleanup()

    assert_equal("agent_message_route", message_route.route_intent, "agent_message")
    assert_equal("agent_message_confirm_not_called", calls["confirm"], 0)
    assert_equal("agent_message_confirmation_planner", message_route.route_confirmation["planner"], "runtime")


def test_direct_answer_can_be_corrected_when_side_effect_is_required() -> None:
    tmp, run = make_run("问下MCAgent乌托邦整合包还缺哪些东西 你去网上找补给他")
    calls = {"confirm": 0}
    try:
        service = AgentToolRouterService(
            decide_tool=lambda *args, **kwargs: {"tool": "direct_answer", "reason": "claims it can relay"},
            confirm_next_step=lambda *args, **kwargs: calls.__setitem__("confirm", calls["confirm"] + 1)
            or {
                "proceed": False,
                "tool": "direct_answer",
                "suggested_tool": "planned_workflow",
                "goal": "ask MCagent then delegate Crawler",
                "reason": "side effect required",
                "action_plan": [
                    {"step": 1, "tool": "mcagent_context", "goal": "ask MCagent for local gaps"},
                    {"step": 2, "tool": "delegate_crawler", "goal": "collect missing public material"},
                ],
            },
            action_plan_has_tool=lambda action_plan, tool: any(item.get("tool") == tool for item in action_plan if isinstance(item, dict)),
        )
        route = service.route(run, session_summary={})
    finally:
        tmp.cleanup()

    assert_equal("confirm_called", calls["confirm"], 1)
    assert_equal("corrected_route_executes_answer_branch", route.route_intent, "answer")
    assert_true("planned_workflow", route.planned_workflow)
    assert_true("planned_delegate", route.planned_delegate)
    assert_equal("replacement_plan_steps", [item["tool"] for item in route.action_plan], ["mcagent_context", "delegate_crawler"])
    assert_equal("confirmation_reason", route.route_confirmation["reason"], "side effect required")


def test_router_marks_planned_delegate_without_executing_it() -> None:
    plan: list[dict[str, Any]] = [
        {"step": 1, "tool": "local_rag_search", "goal": "summarize local data"},
        {"step": 2, "tool": "delegate_crawler", "goal": "collect gaps"},
    ]
    tmp, run = make_run("本地有什么，缺什么让 Crawler 去找")
    try:
        service = AgentToolRouterService(
            decide_tool=lambda *args, **kwargs: {"tool": "planned_workflow", "reason": "two step", "action_plan": plan},
            confirm_next_step=lambda *args, **kwargs: {"proceed": True, "tool": kwargs["proposed_tool"], "goal": "ok", "reason": "confirmed"},
            action_plan_has_tool=lambda action_plan, tool: any(item.get("tool") == tool for item in action_plan if isinstance(item, dict)),
        )
        route = service.route(run, session_summary={})
    finally:
        tmp.cleanup()

    assert_equal("planned_route_executes_answer_first", route.route_intent, "answer")
    assert_true("planned_workflow", route.planned_workflow)
    assert_true("planned_delegate", route.planned_delegate)
    assert_equal("action_plan_kept", route.action_plan, plan)


def test_crawler_collection_request_reuses_agent_decision_confirmation() -> None:
    tmp, run = make_crawler_collection_run("采集乌托邦整合包公开资料并补入 RAG")
    calls = {"confirm": 0}
    try:
        service = AgentToolRouterService(
            decide_tool=lambda *args, **kwargs: {
                "tool": "delegate_crawler",
                "reason": "CrawlerAgent chose background collection after reading the AgentMessage.",
                "collection_target": "采集乌托邦整合包公开资料并补入 RAG",
                "delivery_target": "MCagent/RAG",
            },
            confirm_next_step=lambda *args, **kwargs: calls.__setitem__("confirm", calls["confirm"] + 1) or {"proceed": True},
            action_plan_has_tool=lambda _plan, _tool: False,
        )
        route = service.route(run, session_summary={})
    finally:
        tmp.cleanup()

    assert_equal("route", route.route_intent, "delegate_crawler")
    assert_equal("confirm_not_called", calls["confirm"], 0)
    assert_true("reused_agent_decision", bool(route.route_confirmation.get("reused_agent_decision")), str(route.route_confirmation))
    assert_equal("planner", route.route_confirmation.get("planner"), "runtime_reused_agent_decision")
    selected = [step for step in run.trace.steps if step["stage"] == "decide" and step["status"] == "tool_selected"][0]
    assert_true("decision_elapsed", "elapsed_ms" in selected["detail"], str(selected))


class FakeClient:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[dict[str, Any]] = []

    def chat(self, messages, *, temperature=None, max_tokens=None):  # noqa: ANN001
        self.calls.append({"messages": messages, "temperature": temperature, "max_tokens": max_tokens})
        return self.text


class SequencedFakeClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def chat(self, messages, *, temperature=None, max_tokens=None):  # noqa: ANN001
        self.calls.append({"messages": messages, "temperature": temperature, "max_tokens": max_tokens})
        if not self.responses:
            raise AssertionError("No fake responses left")
        return self.responses.pop(0)


def test_llm_router_owns_prompt_and_json_parsing() -> None:
    tmp, run = make_run("你好")
    fake = FakeClient('```json\n{"tool":"direct_answer","reason":"greeting"}\n```')
    try:
        service = LlmAgentToolRouterService(
            select_client=lambda _config, _model, _temperature: (fake, "fake-router"),
            action_plan_has_tool=lambda _plan, _tool: False,
        )
        decision = service.decide_tool(
            run.config,
            run.payload,
            agent=run.agent,
            original_question=run.original_question,
            contextual_question=run.question,
            session_summary={},
            model=run.model,
        )
    finally:
        tmp.cleanup()

    assert_equal("llm_router_tool", decision["tool"], "direct_answer")
    assert_equal("llm_router_planner", decision["planner"], "fake-router")
    assert_true("llm_router_prompt_mentions_catalog", "Agent Runtime" in fake.calls[0]["messages"][1]["content"])
    assert_equal("json_parser", json_object_from_llm_text("prefix {\"ok\": true} suffix"), {"ok": True})


def test_llm_router_allows_local_corpus_inventory_tool() -> None:
    tmp, run = make_run("本地都有哪些整合包和模组的资料 都简单介绍一下")
    fake = FakeClient('{"tool":"local_corpus_inventory","reason":"inspect local corpus coverage"}')
    try:
        service = LlmAgentToolRouterService(
            select_client=lambda _config, _model, _temperature: (fake, "fake-router"),
            action_plan_has_tool=lambda _plan, _tool: False,
        )
        decision = service.decide_tool(
            run.config,
            run.payload,
            agent=run.agent,
            original_question=run.original_question,
            contextual_question=run.question,
            session_summary={},
            model=run.model,
        )
    finally:
        tmp.cleanup()

    assert_equal("inventory_tool", decision["tool"], "local_corpus_inventory")
    assert_true("inventory_in_catalog", "local_corpus_inventory" in fake.calls[0]["messages"][1]["content"])


def test_llm_router_preserves_information_needs_in_route_and_trace() -> None:
    tmp, run = make_run("Which local modpack should I try first?")
    fake = FakeClient(
        json.dumps(
            {
                "tool": "answer",
                "reason": "The agent needs observations before judging.",
                "information_needs": [
                    {
                        "need_type": "candidate_set",
                        "scope": "local modpack corpus",
                        "reason": "The answer must choose from local candidates.",
                        "observation_hint": "inventory",
                    },
                    {
                        "need_type": "candidate_attributes",
                        "scope": "candidate modpacks",
                        "reason": "The candidates need comparable attributes.",
                    },
                ],
            }
        )
    )
    try:
        service = LlmAgentToolRouterService(
            select_client=lambda _config, _model, _temperature: (fake, "fake-router"),
            action_plan_has_tool=lambda _plan, _tool: False,
        )
        route = service.route(run, session_summary={})
    finally:
        tmp.cleanup()

    assert_equal("route_tool", route.route_intent, "answer")
    assert_equal("route_need_count", len(route.information_needs), 2)
    assert_equal("tool_decision_need", route.tool_decision["information_needs"][0]["need_type"], "candidate_set")
    statuses = [(step["stage"], step["status"]) for step in run.trace.steps]
    assert_true("information_needs_trace", ("plan", "information_needs") in statuses, str(statuses))
    assert_true("prompt_mentions_information_needs", "information_needs" in fake.calls[0]["messages"][1]["content"])


def test_llm_router_reviews_missed_cross_agent_message_route() -> None:
    tmp, run = make_run("麻烦问一下 CrawlerAgent：1+1 等于几")
    fake = SequencedFakeClient(
        [
            '{"tool":"direct_answer","reason":"simple arithmetic"}',
            '{"should_send":true,"to_agent":"CrawlerAgent","content":"请回答用户的问题：1+1 等于几","intent":"agent_message","reason":"user asked MCagent to ask CrawlerAgent"}',
        ]
    )
    try:
        service = LlmAgentToolRouterService(
            select_client=lambda _config, _model, _temperature: (fake, "fake-router"),
            action_plan_has_tool=lambda _plan, _tool: False,
        )
        route = service.route(run, session_summary={})
    finally:
        tmp.cleanup()

    assert_equal("corrected_route", route.route_intent, "agent_message")
    assert_equal("corrected_to_agent", route.tool_decision["to_agent"], "CrawlerAgent")
    assert_true("corrected_content", "1+1" in route.tool_decision["content"], str(route.tool_decision))
    assert_equal("review_call_count", len(fake.calls), 2)
    statuses = [(step["stage"], step["status"]) for step in run.trace.steps]
    assert_true("correction_trace", ("decide", "cross_agent_message_route_corrected") in statuses, str(statuses))


def test_llm_router_cross_agent_review_can_decline_ordinary_mentions() -> None:
    tmp, run = make_run("解释一下 crawler 这个英文单词是什么意思")
    fake = SequencedFakeClient(
        [
            '{"tool":"direct_answer","reason":"ordinary word explanation"}',
        ]
    )
    try:
        service = LlmAgentToolRouterService(
            select_client=lambda _config, _model, _temperature: (fake, "fake-router"),
            action_plan_has_tool=lambda _plan, _tool: False,
        )
        route = service.route(run, session_summary={})
    finally:
        tmp.cleanup()

    assert_equal("keeps_direct_answer", route.route_intent, "direct_answer")
    assert_equal("ordinary_mention_uses_only_agent_tool_selection_llm", len(fake.calls), 1)
    assert_equal("runtime_preflight", route.route_confirmation.get("planner"), "runtime_preflight")


def test_mcagent_router_exposes_crawler_contact_only_as_agent_message() -> None:
    tmp, run = make_run("让 Crawler 去采集落幕曲新手攻略")
    fake = FakeClient(
        '{"tool":"agent_message","reason":"send the request over the only cross-agent message bus",'
        '"to_agent":"CrawlerAgent","content":"请根据用户请求采集落幕曲新手攻略资料，并由你自行决定是否启动采集。","intent":"collection_request"}'
    )
    try:
        service = LlmAgentToolRouterService(
            select_client=lambda _config, _model, _temperature: (fake, "fake-router"),
            action_plan_has_tool=lambda _plan, _tool: False,
        )
        decision = service.decide_tool(
            run.config,
            run.payload,
            agent=run.agent,
            original_question=run.original_question,
            contextual_question=run.question,
            session_summary={},
            model=run.model,
        )
    finally:
        tmp.cleanup()

    prompt = fake.calls[0]["messages"][1]["content"]
    assert_equal("mcagent_crawler_contact_tool", decision["tool"], "agent_message")
    assert_equal("mcagent_crawler_contact_to", decision["to_agent"], "CrawlerAgent")
    assert_true("mcagent_prompt_no_delegate_tool", "delegate_crawler: Send a natural-language Minecraft data collection task" not in prompt, prompt)
    assert_true("mcagent_prompt_only_agent_message", "MCagent has no separate crawler delegation tool" in prompt or "只能选择 agent_message" in prompt, prompt)


def test_llm_router_repairs_malformed_json_before_router_error() -> None:
    tmp, run = make_run("hello")
    fake = SequencedFakeClient(
        [
            '{"tool":"planned_workflow","reason":"needs two steps" "action_plan":[{"step":1,"tool":"mcagent_context","goal":"inspect gaps"},{"step":2,"tool":"delegate_crawler","goal":"collect"}]}',
            '{"tool":"planned_workflow","reason":"needs two steps","action_plan":[{"step":1,"tool":"mcagent_context","goal":"inspect gaps"},{"step":2,"tool":"delegate_crawler","goal":"collect"}],"delivery_target":"MCagent/RAG"}',
        ]
    )
    try:
        service = LlmAgentToolRouterService(
            select_client=lambda _config, _model, _temperature: (fake, "fake-router"),
            action_plan_has_tool=lambda _plan, _tool: False,
        )
        decision = service.decide_tool(
            run.config,
            run.payload,
            agent=run.agent,
            original_question=run.original_question,
            contextual_question=run.question,
            session_summary={},
            model=run.model,
        )
    finally:
        tmp.cleanup()

    assert_equal("repaired_tool", decision["tool"], "planned_workflow")
    assert_equal("repair_call_count", len(fake.calls), 2)
    assert_true("repair_prompt", "Repair it" in fake.calls[1]["messages"][1]["content"])


def test_llm_router_retries_compact_decision_when_repair_fails() -> None:
    tmp, run = make_run("问下MCAgent乌托邦整合包还缺哪些东西 你去网上找补给他")
    fake = SequencedFakeClient(
        [
            '{"tool":"planned_workflow","reason":"truncated',
            '{"tool":"planned_workflow","reason":"still bad"',
            '{"tool":"planned_workflow","reason":"retry ok","action_plan":[{"step":1,"tool":"mcagent_context","goal":"inspect gaps"},{"step":2,"tool":"delegate_crawler","goal":"collect"}],"delivery_target":"MCagent/RAG"}',
        ]
    )
    try:
        service = LlmAgentToolRouterService(
            select_client=lambda _config, _model, _temperature: (fake, "fake-router"),
            action_plan_has_tool=lambda _plan, _tool: False,
        )
        decision = service.decide_tool(
            run.config,
            run.payload,
            agent="crawler_agent",
            original_question=run.original_question,
            contextual_question=run.question,
            session_summary={},
            model=run.model,
        )
    finally:
        tmp.cleanup()

    assert_equal("retry_tool", decision["tool"], "planned_workflow")
    assert_equal("retry_call_count", len(fake.calls), 3)
    assert_true("compact_retry_prompt", "Repeat the decision from scratch" in fake.calls[2]["messages"][1]["content"])


def test_llm_router_selects_client_for_active_agent() -> None:
    tmp, run = make_run("请 CrawlerAgent 采集公开资料")
    fake = FakeClient('{"tool":"delegate_crawler","reason":"collect public data","collection_target":"公开资料","delivery_target":"MCagent/RAG"}')
    seen_agents: list[str] = []

    seen_timeouts: list[int] = []

    def select_client(_config, _model, _temperature, *, agent, timeout_seconds=None):  # noqa: ANN001
        seen_agents.append(agent)
        seen_timeouts.append(int(timeout_seconds or 0))
        return fake, f"{agent}-router"

    try:
        service = LlmAgentToolRouterService(
            select_client=select_client,
            action_plan_has_tool=lambda _plan, _tool: False,
        )
        decision = service.decide_tool(
            run.config,
            run.payload,
            agent="crawler_agent",
            original_question=run.original_question,
            contextual_question=run.question,
            session_summary={},
            model=run.model,
        )
    finally:
        tmp.cleanup()

    assert_equal("selector_agent", seen_agents, ["crawler_agent"])
    assert_true("bounded_router_timeout", 1 <= seen_timeouts[0] <= 60)
    assert_equal("planner_label", decision["planner"], "crawler_agent-router")


def test_llm_router_error_does_not_choose_fallback_tool() -> None:
    tmp, run = make_run("介绍乌托邦")
    try:
        service = LlmAgentToolRouterService(
            select_client=lambda _config, _model, _temperature: (_raise(RuntimeError("boom")), "never"),
            action_plan_has_tool=lambda _plan, _tool: False,
        )
        decision = service.decide_tool(
            run.config,
            run.payload,
            agent=run.agent,
            original_question=run.original_question,
            contextual_question=run.question,
            session_summary={},
            model=run.model,
        )
    finally:
        tmp.cleanup()

    assert_equal("router_error_tool", decision["tool"], "router_error")
    assert_true("router_error_no_delegate", decision.get("delivery_target", "") == "")


def _raise(exc: Exception) -> None:
    raise exc


def main() -> int:
    test_router_records_decision_and_confirmation()
    test_router_respects_agent_suggested_tool()
    test_router_error_stops_before_confirmation()
    test_no_persistence_routes_skip_second_llm_confirmation()
    test_direct_answer_can_be_corrected_when_side_effect_is_required()
    test_router_marks_planned_delegate_without_executing_it()
    test_crawler_collection_request_reuses_agent_decision_confirmation()
    test_llm_router_owns_prompt_and_json_parsing()
    test_llm_router_allows_local_corpus_inventory_tool()
    test_llm_router_preserves_information_needs_in_route_and_trace()
    test_llm_router_reviews_missed_cross_agent_message_route()
    test_llm_router_cross_agent_review_can_decline_ordinary_mentions()
    test_mcagent_router_exposes_crawler_contact_only_as_agent_message()
    test_llm_router_repairs_malformed_json_before_router_error()
    test_llm_router_retries_compact_decision_when_repair_fails()
    test_llm_router_selects_client_for_active_agent()
    test_llm_router_error_does_not_choose_fallback_tool()
    print("AGENT ROUTER SCENARIOS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
