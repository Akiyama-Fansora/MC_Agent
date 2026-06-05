from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Callable

from .config import AppConfig


DelegationHandoffFn = Callable[[dict[str, Any], str, str], dict[str, str]]
InferDeliveryTargetFn = Callable[[str, dict[str, Any]], str]
BuildHandoffBriefFn = Callable[..., tuple[str, str]]


@dataclass(slots=True)
class CrawlerDelegationPlan:
    collection_question: str
    requested_by: str
    delivery_target: str
    handoff_brief: str
    brief_reason: str
    planner_summary: dict[str, Any]
    delegate_payload: dict[str, Any]


class CrawlerDelegationService:
    """Prepare the MCagent-to-CrawlerAgent handoff package.

    The service does not decide whether a Crawler task should exist, and it does
    not decide how CrawlerAgent should search. It only preserves the relationship
    and context once MCagent has already selected a Crawler delegation path.
    """

    def __init__(
        self,
        *,
        delegation_handoff: DelegationHandoffFn,
        infer_delivery_target: InferDeliveryTargetFn,
        build_handoff_brief: BuildHandoffBriefFn,
    ) -> None:
        self._delegation_handoff = delegation_handoff
        self._infer_delivery_target = infer_delivery_target
        self._build_handoff_brief = build_handoff_brief

    def prepare(
        self,
        config: AppConfig,
        payload: dict[str, Any],
        *,
        model: str,
        original_question: str,
        current_question: str,
        collection_target: str,
        session_summary: dict[str, Any],
        gap_summary: str = "",
        planning_instruction: str = "",
        delivery_target: str = "",
    ) -> CrawlerDelegationPlan:
        collection_question = str(collection_target or original_question or current_question).strip()
        handoff = self._delegation_handoff(payload, original_question, collection_question)
        requested_by = handoff["requested_by"]
        explicit_payload_delivery = str(payload.get("delivery_target") or "").strip()
        resolved_delivery_target = str(explicit_payload_delivery or delivery_target or "").strip()
        if not explicit_payload_delivery and "mcagent/rag" in resolved_delivery_target.lower():
            resolved_delivery_target = "MCagent/RAG"
        if (
            requested_by in {"mcagent", "user_via_mcagent"}
            and not explicit_payload_delivery
            and resolved_delivery_target.lower() == "human"
            and _looks_like_mcagent_rag_fill_goal(
                original_question,
                collection_question,
                gap_summary,
                planning_instruction,
                session_summary,
            )
        ):
            resolved_delivery_target = "MCagent/RAG"
        if not resolved_delivery_target:
            resolved_delivery_target = (
                "MCagent/RAG"
                if requested_by in {"mcagent", "user_via_mcagent"}
                else self._infer_delivery_target(original_question, session_summary)
            )

        planner_summary = dict(session_summary or {})
        planner_summary["collection_target"] = collection_question
        if gap_summary:
            planner_summary["mcagent_gap_summary"] = gap_summary
        if planning_instruction:
            planner_summary["planning_instruction"] = planning_instruction

        message = payload.get("agent_message") if isinstance(payload.get("agent_message"), dict) else {}
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        existing_brief = str(planner_summary.get("handoff_brief") or metadata.get("handoff_brief") or "").strip()
        if existing_brief:
            handoff_brief = existing_brief
            brief_reason = "Reused handoff_brief carried by the From-Content-To AgentMessage."
        else:
            handoff_brief = self._contract_handoff_brief(
                original_question=original_question,
                collection_question=collection_question,
                requested_by=requested_by,
                resolved_delivery_target=resolved_delivery_target,
                session_summary=planner_summary,
                gap_summary=gap_summary,
                planning_instruction=planning_instruction,
            )
            brief_reason = "Built deterministic handoff contract from MCagent's already-selected delegation context."
        planner_summary["handoff_brief"] = handoff_brief
        planner_summary["handoff_brief_reason"] = brief_reason
        if not planner_summary.get("current_topic") and (planner_summary.get("topics") or []):
            planner_summary["current_topic"] = str((planner_summary.get("topics") or [""])[0])
        if not planner_summary.get("missing_evidence") and (planner_summary.get("gaps") or []):
            planner_summary["missing_evidence"] = "；".join(str(item) for item in (planner_summary.get("gaps") or [])[:8])

        delegate_payload = payload | {
            "requested_by": requested_by,
            "handoff_from": handoff["handoff_from"],
            "original_user_request": handoff["original_user_request"],
            "delivery_target": resolved_delivery_target,
            "preserve_crawler_request": True,
            "session_summary": planner_summary,
        }
        return CrawlerDelegationPlan(
            collection_question=collection_question,
            requested_by=requested_by,
            delivery_target=resolved_delivery_target,
            handoff_brief=handoff_brief,
            brief_reason=brief_reason,
            planner_summary=planner_summary,
            delegate_payload=delegate_payload,
        )

    def _contract_handoff_brief(
        self,
        *,
        original_question: str,
        collection_question: str,
        requested_by: str,
        resolved_delivery_target: str,
        session_summary: dict[str, Any],
        gap_summary: str,
        planning_instruction: str,
    ) -> str:
        caller = {
            "user": "User directly asked CrawlerAgent.",
            "user_via_mcagent": "User asked MCagent to relay this collection request to CrawlerAgent.",
            "mcagent": "MCagent delegated this evidence gap to CrawlerAgent.",
        }.get(requested_by, requested_by or "unknown")
        parts = [
            f"Requested by: {requested_by or 'unknown'}",
            f"Caller relationship: {caller}",
            "From: " + ("MCagent" if requested_by in {"mcagent", "user_via_mcagent"} else "User"),
            "To: CrawlerAgent",
            f"Original user request: {original_question}",
            f"Task goal: {collection_question or original_question}",
            f"Delivery target: {resolved_delivery_target or 'CrawlerAgent decides from task goal'}",
        ]
        topics = [str(item) for item in (session_summary.get("topics") or [])[:6] if str(item).strip()]
        gaps = [str(item) for item in (session_summary.get("gaps") or [])[:8] if str(item).strip()]
        if topics:
            parts.append("Known topics: " + " / ".join(topics))
        if gaps:
            parts.append("Known gaps: " + " / ".join(gaps))
        if gap_summary:
            parts.append("MCagent gap summary:\n" + str(gap_summary)[:4000])
        if planning_instruction:
            parts.append("Planning instruction from MCagent:\n" + str(planning_instruction)[:1500])
        parts.append(
            "Acceptance criteria:\n"
            "- CrawlerAgent must read this context, then independently choose sources, queries, tools, retries, and source audit decisions.\n"
            "- Tools only report objective fetch/download/extract facts; CrawlerAgent decides accept, reject, retry, ignore, or ingest.\n"
            "- If blocked by login, captcha, quota, unavailable archive, or low-quality results, report exact sources, objective reasons, and next viable path."
        )
        return "\n".join(part for part in parts if str(part).strip())[:3500]


def _looks_like_mcagent_rag_fill_goal(*parts: Any) -> bool:
    text = "\n".join(json.dumps(part, ensure_ascii=False, default=str) if isinstance(part, (dict, list)) else str(part or "") for part in parts)
    lowered = text.lower()
    if "mcagent/rag" in lowered or "mcagent" in lowered or "rag" in lowered:
        return True
    patterns = (
        r"补",
        r"补全",
        r"补充",
        r"补给",
        r"补库",
        r"入库",
        r"知识库",
        r"资料库",
        r"缺",
        r"缺口",
        r"missing",
        r"gap",
        r"collect",
        r"fill",
        r"ingest",
    )
    return any(re.search(pattern, lowered, flags=re.I) for pattern in patterns)
