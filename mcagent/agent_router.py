from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Any, Callable, Protocol

from .config import AppConfig
from .agent_runtime import normalize_agent_tool_decision, tool_catalog_prompt, tool_names_for_agent


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
ClientSelectorFn = Callable[[AppConfig, str, float], tuple[Any, str]]


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

        if route_intent == "router_error":
            route_confirmation = {
                "proceed": False,
                "tool": "router_error",
                "goal": "Agent tool selection failed validation; no tool should execute.",
                "reason": str(tool_decision.get("reason") or "Invalid or missing tool selection."),
                "planner": str(tool_decision.get("planner") or "runtime"),
            }
            run.add_trace("decide", "next_step_confirmed", route_confirmation)
            return AgentRouteDecision(
                route_intent=route_intent,
                tool_decision=tool_decision,
                route_confirmation=route_confirmation,
                action_plan=action_plan,
                rag_focus=rag_focus,
                planned_workflow=False,
                planned_delegate=False,
            )

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
            allowed_suggestions = set(tool_names_for_agent(run.agent)) | {"answer", "planned_workflow", "router_error"}
            if suggested_tool in allowed_suggestions:
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


def json_object_from_llm_text(text: str) -> dict[str, Any]:
    stripped = str(text or "").strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    match = re.search(r"\{.*\}", stripped, flags=re.S)
    if match:
        stripped = match.group(0)
    value = json.loads(stripped)
    if not isinstance(value, dict):
        raise ValueError("LLM did not return a JSON object")
    return value


class LlmAgentToolRouterService(AgentToolRouterService):
    def __init__(self, *, select_client: ClientSelectorFn, action_plan_has_tool: ActionPlanHasToolFn) -> None:
        self._select_client = select_client
        super().__init__(
            decide_tool=self.decide_tool,
            confirm_next_step=self.confirm_next_step,
            action_plan_has_tool=action_plan_has_tool,
        )

    def decide_tool(
        self,
        config: AppConfig,
        payload: dict[str, Any],
        *,
        agent: str,
        original_question: str,
        contextual_question: str,
        session_summary: dict[str, Any],
        model: str,
    ) -> dict[str, Any]:
        if agent == "retriever_only" or bool(payload.get("no_llm")):
            return {
                "tool": "answer",
                "reason": "仅检索模式或禁用 LLM，直接进入本地 RAG。",
                "planner": "runtime",
            }
        try:
            client, label = self._select_client(config, model, 0.0)
            catalog = tool_catalog_prompt(agent)
            allowed_tools = "|".join(tool_names_for_agent(agent))
            prompt = (
                "你是当前对话里的 Agent 工具选择器，只决定下一步使用哪个工具，不回答用户问题。\n"
                "参与者：用户、MCagent、CrawlerAgent。\n"
                "下面是本项目统一 Agent Runtime 暴露给当前 Agent 的工具目录。工具目录是能力说明，不是关键词触发规则。\n"
                f"{catalog}\n"
                "角色与工具关系：active_agent 只能从自己的工具目录中选择下一步；工具目录描述能力与副作用，不提供关键词触发规则。\n"
                "交付对象判断：delivery_target 是任务语义的一部分。根据用户目标、会话上下文和工具副作用判断交付给 human、MCagent/RAG 或两者，而不是按固定句式判断。\n"
                "重要原则：不要用关键词触发。必须按语义判断。不要把游戏内“获取某物/如何获得”误判成 Crawler 采集任务。\n"
                "MCagent 的本地 RAG 当前主要服务 Minecraft 资料库；CrawlerAgent 不限于 Minecraft，应按用户给定目标采集合法、可访问的公开资料或本地资料。\n"
                "委托交接原则：collection_target 不是搜索词，也不是给工具的死规则，而是给 CrawlerAgent 的自然语言任务目标。若任务目标依赖上下文，要把相关背景自然写进目标；不要拆成关键词，也不要丢掉用户原话。\n"
                "复合任务可以选择 planned_workflow，并给出 action_plan；简单、可直接回答的内容可以选择 direct_answer。\n"
                "如果需要本地 RAG 检索，rag_focus 要写成真正要查的主题问题，去掉“本地资料、缺什么、让 Crawler 去找、状态”等元指令。\n"
                "只输出 JSON，不要 Markdown，不要解释隐藏思考。\n"
                "额外工具 direct_answer：当用户只是问候、闲聊、询问系统能力、要求解释当前行为，或任何不需要本地资料/状态/Crawler 的问题时选择 direct_answer；选择它就不要触发 local_rag_search。\n"
                f"active_agent: {agent}\n"
                f"original_user_message: {original_question}\n"
                f"contextualized_for_retrieval: {contextual_question}\n"
                f"session_summary: {json.dumps(session_summary or {}, ensure_ascii=False)}\n"
                f"JSON schema: {{\"tool\":\"{allowed_tools}\", "
                "\"reason\":\"一句面向开发日志的理由\", "
                "\"rag_focus\":\"需要本地检索时的主题化检索问题\", "
                "\"collection_target\":\"若选择 delegate_crawler，写成完整自然语言采集目标；不要拆搜索词，也不要丢掉用户原话和必要上下文\", "
                "\"delivery_target\":\"human|MCagent/RAG|\", "
                "\"action_plan\":[{\"step\":1,\"tool\":\"local_rag_search|summarize_gaps|delegate_crawler|crawler_status\",\"goal\":\"这一步要完成什么\"}]}"
            )
            raw_text = client.chat(
                [
                    {"role": "system", "content": "只输出合法 JSON。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
                max_tokens=900,
            )
            value = json_object_from_llm_text(raw_text)
            return normalize_agent_tool_decision(
                value,
                agent_id=agent,
                original_question=original_question,
                planner=label,
            ).to_dict()
        except Exception as exc:  # noqa: BLE001 - do not execute any tool without an Agent decision.
            return {
                "tool": "router_error",
                "reason": f"Agent tool selector failed; no tool executed without an Agent decision: {type(exc).__name__}: {exc}",
                "collection_target": original_question,
                "delivery_target": "",
                "planner": "fallback_after_llm_error",
                "error": f"{type(exc).__name__}: {exc}",
            }

    def confirm_next_step(
        self,
        config: AppConfig,
        payload: dict[str, Any],
        *,
        agent: str,
        model: str,
        original_question: str,
        session_summary: dict[str, Any],
        proposed_tool: str,
        proposed_goal: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if agent == "retriever_only" or bool(payload.get("no_llm")):
            return {"proceed": True, "tool": proposed_tool, "goal": proposed_goal, "reason": "runtime mode confirmed", "planner": "runtime"}
        try:
            client, label = self._select_client(config, model, 0.0)
            catalog = tool_catalog_prompt(agent, include_principles=True)
            allowed_tools = ", ".join(tool_names_for_agent(agent))
            prompt = (
                "你是当前 Agent 的下一步行动确认器。只确认下一步工具动作，不回答用户问题，不写最终答案。\n"
                "必须基于用户原话、会话摘要、上一步决策和当前上下文判断：现在是否应该执行 proposed_tool，目标是否说清楚，是否需要改用另一个允许工具。\n"
                f"当前 Agent 工具目录：\n{catalog}\n"
                f"允许工具名：{allowed_tools}, answer, evidence_select, final_answer_llm。\n"
                "如果 proposed_tool 合理，proceed=true；如果不合理，proceed=false 并给出 suggested_tool。不要拆 Crawler 的搜索词，不要替工具生成最终回答。\n"
                "只输出 JSON。\n"
                "额外工具 direct_answer：当用户只是问候、闲聊、询问系统能力、要求解释当前行为，或任何不需要本地资料/状态/Crawler 的问题时选择 direct_answer；选择它就不要触发 local_rag_search。\n"
                f"active_agent: {agent}\n"
                f"original_user_message: {original_question}\n"
                f"session_summary: {json.dumps(session_summary or {}, ensure_ascii=False)}\n"
                f"proposed_tool: {proposed_tool}\n"
                f"proposed_goal: {proposed_goal}\n"
                f"context: {json.dumps(context or {}, ensure_ascii=False)}\n"
                "JSON schema: {\"proceed\":true, \"tool\":\"工具名\", \"suggested_tool\":\"可选\", \"goal\":\"确认后的下一步目标\", \"reason\":\"一句理由\", \"concern\":\"可选风险\"}"
            )
            raw_text = client.chat(
                [
                    {"role": "system", "content": "只输出合法 JSON。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
                max_tokens=700,
            )
            value = json_object_from_llm_text(raw_text)
            tool = str(value.get("tool") or proposed_tool).strip() or proposed_tool
            suggested = str(value.get("suggested_tool") or "").strip()
            return {
                "proceed": bool(value.get("proceed", True)),
                "tool": tool,
                "suggested_tool": suggested,
                "goal": str(value.get("goal") or proposed_goal).strip()[:500],
                "reason": str(value.get("reason") or "Agent confirmed next step.").strip()[:500],
                "concern": str(value.get("concern") or "").strip()[:500],
                "planner": label,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "proceed": True,
                "tool": proposed_tool,
                "goal": proposed_goal,
                "reason": f"Next-step confirmation failed; continuing with prior Agent decision: {type(exc).__name__}: {exc}",
                "planner": "fallback_after_confirmation_error",
            }
