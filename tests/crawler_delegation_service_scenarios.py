from __future__ import annotations

from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.crawler_delegation_service import CrawlerDelegationService  # noqa: E402


def assert_equal(name: str, actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"{name}: expected {expected!r}, got {actual!r}")


def assert_true(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name}: {detail}")


def build_service(calls: list[dict[str, Any]], *, requested_by: str = "user_via_mcagent") -> CrawlerDelegationService:
    def handoff(_payload: dict[str, Any], original_question: str, collection_question: str) -> dict[str, str]:
        calls.append({"fn": "handoff", "original": original_question, "collection": collection_question})
        return {
            "requested_by": requested_by,
            "handoff_from": "MCagent" if requested_by != "user" else "user",
            "original_user_request": original_question,
        }

    def infer(target: str, summary: dict[str, Any]) -> str:
        calls.append({"fn": "infer", "target": target, "summary": summary})
        return "human"

    def brief(config: object, **kwargs: Any) -> tuple[str, str]:
        calls.append({"fn": "brief", **kwargs})
        return "handoff brief", "brief reason"

    return CrawlerDelegationService(
        delegation_handoff=handoff,
        infer_delivery_target=infer,
        build_handoff_brief=brief,
    )


def test_prepare_preserves_mcagent_to_crawler_relationship() -> None:
    calls: list[dict[str, Any]] = []
    service = build_service(calls, requested_by="user_via_mcagent")

    plan = service.prepare(
        object(),
        {"agent": "mcagent_rag"},
        model="model-a",
        original_question="tell Crawler to collect Utopia data",
        current_question="contextualized question",
        collection_target="Utopia modpack missing data",
        session_summary={"topics": ["Utopia"], "gaps": ["boss list", "mod list"]},
        gap_summary="MCagent found gaps",
        planning_instruction="Crawler should plan its own sources",
    )

    assert_equal("requested_by", plan.requested_by, "user_via_mcagent")
    assert_equal("delivery_target", plan.delivery_target, "MCagent/RAG")
    assert_equal("collection_question", plan.collection_question, "Utopia modpack missing data")
    assert_equal("handoff_brief", plan.handoff_brief, "handoff brief")
    assert_equal("payload_requested_by", plan.delegate_payload["requested_by"], "user_via_mcagent")
    assert_equal("payload_handoff_from", plan.delegate_payload["handoff_from"], "MCagent")
    assert_equal("payload_preserve", plan.delegate_payload["preserve_crawler_request"], True)
    assert_equal("summary_gap", plan.planner_summary["mcagent_gap_summary"], "MCagent found gaps")
    assert_equal("summary_instruction", plan.planner_summary["planning_instruction"], "Crawler should plan its own sources")
    assert_equal("summary_current_topic", plan.planner_summary["current_topic"], "Utopia")
    assert_equal("summary_missing", plan.planner_summary["missing_evidence"], "boss list；mod list")
    assert_true("brief_called", any(call.get("fn") == "brief" for call in calls))
    brief_call = next(call for call in calls if call.get("fn") == "brief")
    assert_equal("brief_delivery", brief_call["delivery_target"], "MCagent/RAG")
    assert_equal("brief_gap", brief_call["mcagent_gap_summary"], "MCagent found gaps")


def test_prepare_uses_inferred_delivery_for_direct_user_request() -> None:
    calls: list[dict[str, Any]] = []
    service = build_service(calls, requested_by="user")

    plan = service.prepare(
        object(),
        {"agent": "crawler_agent"},
        model="model-a",
        original_question="collect public prices",
        current_question="collect public prices",
        collection_target="public prices",
        session_summary={},
    )

    assert_equal("requested_by", plan.requested_by, "user")
    assert_equal("delivery_target", plan.delivery_target, "human")
    assert_true("infer_called", any(call.get("fn") == "infer" for call in calls))
    assert_equal("payload_delivery", plan.delegate_payload["delivery_target"], "human")


def test_prepare_corrects_model_human_delivery_for_mcagent_gap_fill() -> None:
    calls: list[dict[str, Any]] = []
    service = build_service(calls, requested_by="user_via_mcagent")

    plan = service.prepare(
        object(),
        {"agent": "mcagent_rag"},
        model="model-a",
        original_question="现在乌托邦整合包本地还缺哪些资料，列出来，然后让 Crawler 去补充。",
        current_question="现在乌托邦整合包本地还缺哪些资料，列出来，然后让 Crawler 去补充。",
        collection_target="现在乌托邦整合包本地还缺哪些资料，列出来，然后让 Crawler 去补充。",
        session_summary={"gaps": ["完整模组列表"]},
        delivery_target="human",
    )

    assert_equal("delivery_target", plan.delivery_target, "MCagent/RAG")
    assert_equal("payload_delivery", plan.delegate_payload["delivery_target"], "MCagent/RAG")
    brief_call = next(call for call in calls if call.get("fn") == "brief")
    assert_equal("brief_delivery", brief_call["delivery_target"], "MCagent/RAG")


def test_prepare_normalizes_mixed_human_rag_delivery_for_mcagent_gap_fill() -> None:
    calls: list[dict[str, Any]] = []
    service = build_service(calls, requested_by="user_via_mcagent")

    plan = service.prepare(
        object(),
        {"agent": "mcagent_rag"},
        model="model-a",
        original_question="collect missing Utopia data for MCagent",
        current_question="collect missing Utopia data for MCagent",
        collection_target="collect missing Utopia data for MCagent/RAG and report to user",
        session_summary={},
        delivery_target="human|MCagent/RAG",
    )

    assert_equal("delivery_target", plan.delivery_target, "MCagent/RAG")
    assert_equal("payload_delivery", plan.delegate_payload["delivery_target"], "MCagent/RAG")


if __name__ == "__main__":
    test_prepare_preserves_mcagent_to_crawler_relationship()
    test_prepare_uses_inferred_delivery_for_direct_user_request()
    test_prepare_corrects_model_human_delivery_for_mcagent_gap_fill()
    test_prepare_normalizes_mixed_human_rag_delivery_for_mcagent_gap_fill()
    print("crawler_delegation_service_scenarios: ok")
