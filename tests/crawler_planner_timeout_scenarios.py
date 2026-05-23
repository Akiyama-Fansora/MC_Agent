from __future__ import annotations

from pathlib import Path
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.config import load_config  # noqa: E402
from mcagent.crawler_llm_planner import plan_crawler_tasks_rule_fallback  # noqa: E402
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
    test_rule_fallback_extracts_domain_target_from_agent_handoff()
    test_job_planner_timeout_returns_executable_fallback()
    print("crawler_planner_timeout_scenarios passed")
