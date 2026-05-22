from __future__ import annotations

from pathlib import Path
import sys
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.agent_execution import build_agent_execution_context  # noqa: E402
from mcagent.agent_router import AgentToolRouterService  # noqa: E402
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


def main() -> int:
    test_router_records_decision_and_confirmation()
    test_router_respects_agent_suggested_tool()
    test_router_marks_planned_delegate_without_executing_it()
    print("AGENT ROUTER SCENARIOS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
