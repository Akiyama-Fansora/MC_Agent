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
    tool_catalog_prompt,
)
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
        ("uncertain", {"source": "jina", "returncode": 0, "uncertain_result": True, "manifest_stats": {"records": 1}}),
        ("duplicate_reused", {"source": "mcmod", "returncode": 0, "existing_evidence_reused": {"matched": True}, "manifest_stats": {"records": 0, "skipped": 3}}),
        ("blocked", {"source": "planner", "returncode": 2, "empty_query_result": True}),
        ("stopped", {"source": "browser_collect", "returncode": 130}),
        ("timeout", {"source": "tavily", "returncode": 124, "timed_out": True}),
        ("quota_limited", {"source": "firecrawl", "returncode": 1, "output": "HTTP 429 quota exceeded"}),
        ("captcha_required", {"source": "browser_collect", "returncode": 1, "output": "captcha verification required"}),
        ("login_required", {"source": "browser_collect", "returncode": 1, "output": "please login or sign in"}),
        ("auth_required", {"source": "firecrawl", "returncode": 1, "output": "HTTP 401 unauthorized"}),
        ("network_error", {"source": "jina", "returncode": 1, "output": "failed to fetch: DNS connection error"}),
        ("parse_error", {"source": "playwright", "returncode": 1, "output": "JSONDecodeError invalid json"}),
        ("execution_error", {"source": "unknown", "returncode": 1, "output": "script failed"}),
    ]
    for expected, result in cases:
        observation = classify_crawler_tool_result(result)
        assert_true(f"known_status_{expected}", observation.status in TOOL_RESULT_STATUSES)
        assert_equal(f"classify_{expected}", observation.status, expected)
        assert_true(f"summary_{expected}", bool(observation.summary))


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
    assert_true("crawler_browser", "browser_collect" in crawler_catalog)
    assert_true("crawler_modpack_internal", "modpack_internal" in crawler_catalog)
    assert_true("llm_ownership", "LLM owns interpretation" in mcagent_catalog)


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
            },
            "planned_tasks": [
                {"source": "mcmod", "query": "乌托邦探险之旅", "reason": "项目页"},
                {"source": "firecrawl", "query": "乌托邦探险之旅 玩法", "reason": "教程页"},
            ],
            "tasks": [
                {
                    "source": "mcmod",
                    "query": "乌托邦探险之旅",
                    "returncode": 0,
                    "manifest_stats": {"records": 2, "skipped": 0, "errors": 0},
                },
                {
                    "source": "firecrawl",
                    "query": "乌托邦探险之旅 玩法",
                    "returncode": 1,
                    "output": "HTTP 429 quota exceeded",
                    "manifest_stats": {"records": 0, "skipped": 0, "errors": 1},
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


def main() -> int:
    test_tool_observation_matrix()
    test_handoff_contract_preserves_context()
    test_tool_catalog_exposes_agent_capabilities()
    test_job_readable_summary_surfaces_observations()
    print("AGENT RUNTIME SCENARIOS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
