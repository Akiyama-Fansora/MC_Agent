from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .config import AppConfig


class TraceCapableRun(Protocol):
    config: AppConfig
    payload: dict[str, Any]
    agent: str
    model: str
    original_question: str
    question: str

    def add_trace(self, stage: str, status: str, detail: Any = None) -> dict[str, Any]:
        ...


DecisionFn = Callable[..., dict[str, Any]]
ConfirmFn = Callable[..., dict[str, Any]]
ActionPlanHasToolFn = Callable[[list[Any], str], bool]


@dataclass(frozen=True, slots=True)
class AgentRouteDecision:
    route_intent: str
    tool_decision: dict[str, Any]
    route_confirmation: dict[str, Any]
    action_plan: list[Any] = field(default_factory=list)
    rag_focus: str = ""
    planned_workflow: bool = False
    planned_delegate: bool = False


class AgentToolRouterService:
    """Owns tool-routing orchestration while leaving LLM judgment to injected functions."""

    def __init__(
        self,
        *,
        decide_tool: DecisionFn,
        confirm_next_step: ConfirmFn,
        action_plan_has_tool: ActionPlanHasToolFn,
    ) -> None:
        self._decide_tool = decide_tool
        self._confirm_next_step = confirm_next_step
        self._action_plan_has_tool = action_plan_has_tool

    def route(self, run: TraceCapableRun, *, session_summary: dict[str, Any]) -> AgentRouteDecision:
        tool_decision = self._decide_tool(
            run.config,
            run.payload,
            agent=run.agent,
            original_question=run.original_question,
            contextual_question=run.question,
            session_summary=session_summary,
            model=run.model,
        )
        route_intent = str(tool_decision.get("tool") or "answer")
        action_plan = tool_decision.get("action_plan") if isinstance(tool_decision.get("action_plan"), list) else []
        rag_focus = str(tool_decision.get("rag_focus") or "").strip()
        run.add_trace("decide", "tool_selected", {"tool": route_intent, "original_question": run.original_question, "decision": tool_decision})
        if action_plan:
            run.add_trace("plan", "created", {"steps": action_plan})
        if rag_focus:
            run.add_trace("plan", "rag_focus", {"question": rag_focus})

        route_confirmation = self._confirm_next_step(
            run.config,
            run.payload,
            agent=run.agent,
            model=run.model,
            original_question=run.original_question,
            session_summary=session_summary,
            proposed_tool=route_intent,
            proposed_goal=str(tool_decision.get("reason") or "确认本轮应执行的工具路径。"),
            context={"tool_decision": tool_decision, "action_plan": action_plan},
        )
        run.add_trace("decide", "next_step_confirmed", route_confirmation)
        if not bool(route_confirmation.get("proceed", True)):
            suggested_tool = str(route_confirmation.get("suggested_tool") or route_confirmation.get("tool") or "").strip()
            if suggested_tool in {"direct_answer", "answer", "planned_workflow", "status", "delegate_crawler"}:
                route_intent = suggested_tool

        planned_workflow = route_intent == "planned_workflow"
        planned_delegate = planned_workflow and self._action_plan_has_tool(action_plan, "delegate_crawler")
        if planned_workflow:
            route_intent = "answer"

        return AgentRouteDecision(
            route_intent=route_intent,
            tool_decision=tool_decision,
            route_confirmation=route_confirmation,
            action_plan=action_plan,
            rag_focus=rag_focus,
            planned_workflow=planned_workflow,
            planned_delegate=planned_delegate,
        )
