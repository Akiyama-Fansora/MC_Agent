from __future__ import annotations

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
    test_llm_router_owns_prompt_and_json_parsing()
    test_llm_router_allows_local_corpus_inventory_tool()
    test_llm_router_repairs_malformed_json_before_router_error()
    test_llm_router_retries_compact_decision_when_repair_fails()
    test_llm_router_error_does_not_choose_fallback_tool()
    print("AGENT ROUTER SCENARIOS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
