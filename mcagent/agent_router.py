from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
import time
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
ClientSelectorFn = Callable[..., tuple[Any, str]]
ROUTER_LLM_TIMEOUT_SECONDS = 60


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

    def _review_cross_agent_message_miss(
        self,
        run: TraceCapableRun,
        *,
        session_summary: dict[str, Any],
        tool_decision: dict[str, Any],
        route_intent: str,
    ) -> dict[str, Any] | None:
        return None

    def route(self, run: TraceCapableRun, *, session_summary: dict[str, Any]) -> AgentRouteDecision:
        decision_started = time.time()
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
        run.add_trace(
            "decide",
            "tool_selected",
            {
                "tool": route_intent,
                "original_question": run.original_question,
                "decision": tool_decision,
                "elapsed_ms": round((time.time() - decision_started) * 1000),
            },
        )
        if action_plan:
            run.add_trace("plan", "created", {"steps": action_plan})
        if rag_focus:
            run.add_trace("plan", "rag_focus", {"question": rag_focus})

        review_decision = self._review_cross_agent_message_miss(
            run,
            session_summary=session_summary,
            tool_decision=tool_decision,
            route_intent=route_intent,
        )
        if review_decision:
            route_intent = str(review_decision.get("tool") or "agent_message")
            tool_decision = review_decision
            action_plan = tool_decision.get("action_plan") if isinstance(tool_decision.get("action_plan"), list) else []
            rag_focus = str(tool_decision.get("rag_focus") or "").strip()
            run.add_trace(
                "decide",
                "cross_agent_message_route_corrected",
                {
                    "tool": route_intent,
                    "original_question": run.original_question,
                    "decision": tool_decision,
                },
            )

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

        if route_intent in {"temporary_extract", "status", "agent_message"}:
            route_confirmation = {
                "proceed": True,
                "tool": route_intent,
                "suggested_tool": "",
                "goal": str(tool_decision.get("reason") or "Execute selected no-persistence route."),
                "reason": "No persistent side effect is introduced by this route; the Agent tool decision is sufficient.",
                "concern": "",
                "planner": "runtime",
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

        if self._can_reuse_collection_request_decision(run, route_intent=route_intent, action_plan=action_plan):
            route_confirmation = {
                "proceed": True,
                "tool": route_intent,
                "suggested_tool": "",
                "goal": str(tool_decision.get("reason") or "Execute the CrawlerAgent-selected collection route."),
                "reason": (
                    "CrawlerAgent already selected this collection route after receiving a From-Content-To "
                    "collection_request AgentMessage; runtime reuses that Agent decision instead of asking a "
                    "second LLM to confirm the same side effect."
                ),
                "concern": "",
                "planner": "runtime_reused_agent_decision",
                "reused_agent_decision": True,
            }
            run.add_trace("decide", "next_step_confirmed", route_confirmation)
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

        confirmation_started = time.time()
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
        route_confirmation = dict(route_confirmation or {})
        route_confirmation.setdefault("elapsed_ms", round((time.time() - confirmation_started) * 1000))
        run.add_trace("decide", "next_step_confirmed", route_confirmation)
        if not bool(route_confirmation.get("proceed", True)):
            suggested_tool = str(route_confirmation.get("suggested_tool") or route_confirmation.get("tool") or "").strip()
            allowed_suggestions = set(tool_names_for_agent(run.agent)) | {"answer", "planned_workflow", "router_error"}
            if suggested_tool in allowed_suggestions:
                route_intent = suggested_tool
                confirmed_plan = route_confirmation.get("action_plan")
                if isinstance(confirmed_plan, list) and confirmed_plan:
                    action_plan = confirmed_plan
                    run.add_trace("plan", "confirmed_replacement", {"steps": action_plan})

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

    def _can_reuse_collection_request_decision(
        self,
        run: TraceCapableRun,
        *,
        route_intent: str,
        action_plan: list[Any],
    ) -> bool:
        if run.agent != "crawler_agent":
            return False
        if route_intent == "delegate_crawler":
            selected_collection_route = True
        elif route_intent == "planned_workflow" and self._action_plan_has_tool(action_plan, "delegate_crawler"):
            selected_collection_route = True
        else:
            selected_collection_route = False
        if not selected_collection_route:
            return False
        raw_message = run.payload.get("agent_message")
        if not isinstance(raw_message, dict):
            return False
        metadata = raw_message.get("metadata") if isinstance(raw_message.get("metadata"), dict) else {}
        return (
            str(raw_message.get("to_agent_id") or run.payload.get("agent") or "") == "crawler_agent"
            and (
                str(raw_message.get("intent") or "") == "collection_request"
                or str(metadata.get("tool") or "") == "delegate_crawler"
            )
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


def repair_json_object_from_llm_text(client: Any, text: str, *, schema_hint: str, max_tokens: int = 700) -> dict[str, Any]:
    prompt = (
        "The previous model output was intended to be one JSON object, but it was invalid. "
        "Repair it into exactly one valid JSON object matching this schema. "
        "Do not add Markdown, comments, or explanations.\n"
        f"Schema hint: {schema_hint}\n"
        f"Broken output:\n{str(text or '')[:6000]}"
    )
    repaired = client.chat(
        [
            {"role": "system", "content": "Output only valid JSON."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.0,
        max_tokens=max_tokens,
    )
    return json_object_from_llm_text(repaired)


def retry_json_object_with_compact_prompt(client: Any, *, task_prompt: str, schema_hint: str, error: str, max_tokens: int = 900) -> dict[str, Any]:
    prompt = (
        "Your previous JSON output could not be parsed. Repeat the decision from scratch as exactly one valid JSON object. "
        "No Markdown, no prose, no hidden reasoning.\n"
        f"Parse error: {error}\n"
        f"Schema hint: {schema_hint}\n"
        f"Decision task:\n{task_prompt[:5000]}"
    )
    raw = client.chat(
        [
            {"role": "system", "content": "Output exactly one valid JSON object."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.0,
        max_tokens=max_tokens,
    )
    return json_object_from_llm_text(raw)


class LlmAgentToolRouterService(AgentToolRouterService):
    def __init__(self, *, select_client: ClientSelectorFn, action_plan_has_tool: ActionPlanHasToolFn) -> None:
        self._select_client = select_client
        super().__init__(
            decide_tool=self.decide_tool,
            confirm_next_step=self.confirm_next_step,
            action_plan_has_tool=action_plan_has_tool,
        )

    def _client_for_agent(self, config: AppConfig, model: str, temperature: float, agent: str) -> tuple[Any, str]:
        try:
            return self._select_client(config, model, temperature, agent=agent, timeout_seconds=ROUTER_LLM_TIMEOUT_SECONDS)
        except TypeError:
            return self._select_client(config, model, temperature)

    def _review_cross_agent_message_miss(
        self,
        run: TraceCapableRun,
        *,
        session_summary: dict[str, Any],
        tool_decision: dict[str, Any],
        route_intent: str,
    ) -> dict[str, Any] | None:
        if route_intent in {"agent_message", "router_error", "status", "temporary_extract"}:
            return None
        if "agent_message" not in tool_names_for_agent(run.agent):
            return None
        current_text = f"{run.original_question}\n{run.question}"
        summary_text = json.dumps(session_summary or {}, ensure_ascii=False)
        text = f"{current_text}\n{summary_text}"
        mentions_agent = re.search(r"\b(?:crawler|crawleragent|mcagent|mc agent)\b|CrawlerAgent|MCagent|爬虫|另一个\s*Agent|对方\s*Agent", text, flags=re.I)
        asks_to_message = re.search(
            r"(问|问下|问问|询问|请教|告诉|通知|转达|发给|发送给|让.*(?:回答|说|看|处理)|叫.*(?:回答|看|处理)|ask|tell|message|send to)",
            current_text,
            flags=re.I,
        )
        asks_about_protocol = re.search(
            r"(什么(?:意思|区别)|解释|介绍|协议|机制|函数|工具|能力|是谁|是什么|是不是|怎么实现|怎么做到|how|what is)",
            current_text,
            flags=re.I,
        )
        correction_context = re.search(r"(我就是|不是让你|需要你|去问|问它|问他|问那个)", current_text) and re.search(
            r"\b(?:crawler|crawleragent|mcagent|mc agent)\b|CrawlerAgent|MCagent|爬虫|Agent",
            text,
            flags=re.I,
        )
        if asks_about_protocol and not correction_context:
            return None
        if not (mentions_agent and (asks_to_message or correction_context)):
            return None
        try:
            client, label = self._client_for_agent(run.config, run.model, 0.0, run.agent)
            prompt = (
                "你是跨 Agent 消息漏判复核器，只判断当前 Agent 是否应该把用户消息通过 AgentMessage 发给另一个已知 Agent。\n"
                "参与者只有 User、MCagent、CrawlerAgent。AgentMessage 只投递自然语言消息，不启动采集、不保存、不替接收方选择工具。\n"
                "如果用户只是把 MCagent/Crawler 当普通词、网页内容、标题、代码名、或问一个当前 Agent 可以自己回答的问题，should_send=false。\n"
                "如果用户是在要求当前 Agent 去问、通知、转达、请教另一个 Agent，或在纠正上一轮说“我就是要你去问它/那个 Agent”，should_send=true。\n"
                "content 必须是给接收方的自然语言原始消息，保留用户目标和必要上下文，不拆成搜索词。\n"
                "只输出 JSON。\n"
                f"active_agent: {run.agent}\n"
                f"initial_tool_decision: {json.dumps(tool_decision, ensure_ascii=False)}\n"
                f"original_user_message: {run.original_question}\n"
                f"contextualized_message: {run.question}\n"
                f"session_summary: {json.dumps(session_summary or {}, ensure_ascii=False)}\n"
                'JSON schema: {"should_send":true, "to_agent":"MCagent|CrawlerAgent|User", "content":"message body", "intent":"agent_message|collection_request|context_request", "reason":"short reason"}'
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
        except Exception:
            return None
        if not bool(value.get("should_send")):
            return None
        to_agent = str(value.get("to_agent") or "").strip()
        if to_agent not in {"MCagent", "CrawlerAgent", "User"}:
            return None
        if (run.agent == "mcagent_rag" and to_agent == "MCagent") or (run.agent == "crawler_agent" and to_agent == "CrawlerAgent"):
            return None
        content = str(value.get("content") or run.original_question or run.question).strip()
        if not content:
            return None
        normalized = normalize_agent_tool_decision(
            {
                "tool": "agent_message",
                "reason": str(value.get("reason") or "Cross-Agent message review corrected a missed AgentMessage route."),
                "to_agent": to_agent,
                "content": content,
                "intent": str(value.get("intent") or "agent_message"),
            },
            agent_id=run.agent,
            original_question=run.original_question,
            planner=f"{label}:cross_agent_message_review",
        ).to_dict()
        normalized["reviewed_from_tool"] = route_intent
        return normalized

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
            client, label = self._client_for_agent(config, model, 0.0, agent)
            catalog = tool_catalog_prompt(agent)
            allowed_tools = "|".join(tool_names_for_agent(agent))
            allowed_step_tools = "|".join(name for name in tool_names_for_agent(agent) if name != "planned_workflow")
            mcagent_identity_note = ""
            if agent == "mcagent_rag":
                mcagent_identity_note = (
                    "\nMCagent self-check before choosing a tool:\n"
                    "1. Interpret the message as a Minecraft knowledge assistant first.\n"
                    "2. Decide whether the user is asking about Minecraft content, modpacks, mods, items, Bosses, gameplay, servers, versions, guides, or the local Minecraft knowledge base.\n"
                    "3. If yes, consider local_rag_search/evidence workflow. If the user explicitly asks CrawlerAgent to collect/fill/check something, MCagent can only send an agent_message to CrawlerAgent; CrawlerAgent then decides its own tools.\n"
                    "4. If no, use direct_answer or explain the boundary. This is semantic role reasoning, not keyword matching.\n"
                )
            prompt = (
                "你是当前对话里的 Agent 工具选择器，只决定下一步使用哪个工具，不回答用户问题。\n"
                "参与者：用户、MCagent、CrawlerAgent。任意一轮沟通都可以表示为 AgentMessage(from_agent, content, to_agent)。\n"
                "下面是本项目统一 Agent Runtime 暴露给当前 Agent 的工具目录。工具目录是能力说明，不是关键词触发规则。\n"
                f"{catalog}\n"
                "角色与工具关系：active_agent 只能从自己的工具目录中选择下一步；工具目录描述能力与副作用，不提供关键词触发规则。\n"
                "交付对象判断：delivery_target 是任务语义的一部分。根据用户目标、会话上下文和工具副作用判断交付给 human、MCagent/RAG 或两者，而不是按固定句式判断。若用户让当前 Agent 先询问某个 Agent，再“补给他/交给他/给它用”，代词通常指向刚被询问或被转达的 Agent；例如先问 MCagent 再补给他，应交付给 MCagent/RAG。\n"
                "多轮上下文原则：必须阅读 session_summary.recent_turns、last_user_question、last_assistant_answer 和 recent_agent_events。若用户本轮是在纠正上一轮（例如“我就是需要你去问他/它/ crawler/那个 Agent”），应把本轮要求和上一轮用户问题合并理解；不要把当前短句孤立回答，也不要因为上一轮已经直接回答过就忽略用户纠正。\n"
                "如果 active_agent 是 MCagent：你不是通用关键词路由器，而是 Minecraft 资料 Agent。第一步先按语义判断用户是否在问 Minecraft 相关内容，包括整合包、模组、物品、Boss、玩法、服务器、版本、教程、MC百科/Modrinth/CurseForge、或本地 Minecraft 资料库。若是 MC 相关，优先考虑本地 RAG 证据是否能回答；若用户明确要 CrawlerAgent 采集/补资料，只能选择 agent_message 把自然语言请求发给 CrawlerAgent，由 CrawlerAgent 自己决定是否采集。若明确不是 MC 相关，可 direct_answer 或说明能力边界。不要把这个领域判断写成关键词触发规则，要结合会话上下文和用户真实意图。\n"
                "MCagent 本地资料库问题的工具边界：如果用户在问“本地资料库/本地库/资料覆盖/有哪些资料/有哪些整合包或项目/完整盘点/库存范围”这类关于语料库自身覆盖范围的问题，应优先选择 local_corpus_inventory，因为普通 local_rag_search 只返回相关片段，不能代表全库。只有用户在问某个具体事实、玩法、物品、Boss、配方、版本、攻略或已知主题的证据时，才选择 local_rag_search。这个判断必须基于语义，不要写成固定关键词触发。\n"
                "重要原则：不要用关键词触发。必须按语义判断。不要把游戏内“获取某物/如何获得”误判成 Crawler 采集任务。\n"
                "MCagent 的本地 RAG 当前主要服务 Minecraft 资料库；CrawlerAgent 不限于 Minecraft，应按用户给定目标采集合法、可访问的公开资料或本地资料。\n"
                "CrawlerAgent 工具边界：temporary_extract 是即时读取、抽取、总结且不保存；delegate_crawler 是 CrawlerAgent 自己的后台采集循环入口，通常会产生本地导出或补库。MCagent 看不到也不能直接调用这个入口；MCagent 只能用 agent_message 和 CrawlerAgent 对话。若用户目标明确是只读/只总结且不保存，CrawlerAgent 应选择没有持久化副作用的工具。\n"
                "CrawlerAgent 复合沟通边界：如果 CrawlerAgent 收到用户要求先问 MCagent、查看 MCagent 本地缺口或获取 MCagent 上下文，并且同一目标还要求再补充、采集、入库或交付资料，这不是单独的 mcagent_context，也不是本地 RAG 回答；CrawlerAgent 可选择 planned_workflow，并在 action_plan 中依次包含 mcagent_context 与 delegate_crawler。只有用户只要求问 MCagent、没有后续采集副作用时，才选择 mcagent_context 单步。\n"
                "跨 Agent 沟通原则：消息通道只负责把内容送给目标 Agent，收到消息的 Agent 再根据自己的工具目录判断下一步。若当前 Agent 需要对方能力，选择能完成这次消息投递的工具，例如向 MCagent 询问本地上下文或向 CrawlerAgent 交付采集目标。\n"
                "Cross-Agent communication rule: agent_message is the only From-Content-To communication tool exposed to route a message to another participant. The LLM must semantically decide whether to_agent is User, MCagent, or CrawlerAgent; names such as MCagent/Crawler may also be ordinary words or names. agent_message has no persistence and does not choose the receiver's next tool. MCagent must never describe a separate delegation API/function/scheduler for talking to CrawlerAgent.\n"
                "CrawlerAgent 请求原则：给 CrawlerAgent 的 content 不是搜索词，也不是给工具的死规则，而是给 CrawlerAgent 的自然语言消息。若任务目标依赖上下文，要把相关背景自然写进消息；不要拆成关键词，也不要丢掉用户原话。\n"
                "复合任务可以选择 planned_workflow，并给出 action_plan；简单、可直接回答的内容可以选择 direct_answer。\n"
                "planned_workflow 完整性原则：action_plan 必须覆盖用户请求中的所有可执行动作。若 MCagent 的用户目标要求 CrawlerAgent 采集、补充资料、保存、入库或启动后台工作，MCagent 的计划不能伪造采集工具，只能包含 agent_message to CrawlerAgent；后续采集动作由 CrawlerAgent 收到消息后自己选择。若用户只要求解释或总结，则不要加入跨 Agent 消息。\n"
                "如果需要本地 RAG 检索，rag_focus 要写成真正要查的主题问题，去掉“本地资料、缺什么、让 Crawler 去找、状态”等元指令。\n"
                "只输出 JSON，不要 Markdown，不要解释隐藏思考。\n"
                "额外工具 direct_answer：当用户只是问候、闲聊、询问系统能力、要求解释当前行为，或任何不需要本地资料/状态/Crawler 的问题时选择 direct_answer；选择它就不要触发 local_rag_search。\n"
                f"active_agent: {agent}\n"
                f"{mcagent_identity_note}"
                f"original_user_message: {original_question}\n"
                f"contextualized_for_retrieval: {contextual_question}\n"
                f"session_summary: {json.dumps(session_summary or {}, ensure_ascii=False)}\n"
                f"JSON schema: {{\"tool\":\"{allowed_tools}\", "
                "\"reason\":\"一句面向开发日志的理由\", "
                "\"rag_focus\":\"需要本地检索时的主题化检索问题\", "
                "\"collection_target\":\"若当前 Agent 是 CrawlerAgent 且选择自己的采集工具，写成完整自然语言采集目标；若当前 Agent 是 MCagent，优先用 agent_message.content 给 CrawlerAgent\", "
                "\"delivery_target\":\"human|MCagent/RAG|\", "
                "\"to_agent\":\"User|MCagent|CrawlerAgent; only when tool=agent_message\", "
                "\"content\":\"From-Content-To message body; only when tool=agent_message\", "
                "\"intent\":\"optional semantic intent for agent_message\", "
                f"\"action_plan\":[{{\"step\":1,\"tool\":\"{allowed_step_tools}\",\"goal\":\"这一步要完成什么\"}}]}}"
            )
            raw_text = client.chat(
                [
                    {"role": "system", "content": "只输出合法 JSON。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
                max_tokens=1400,
            )
            schema_hint = '{"tool":"allowed tool name","reason":"why","rag_focus":"","collection_target":"","delivery_target":"human|MCagent/RAG|","to_agent":"User|MCagent|CrawlerAgent","content":"message body","intent":"","action_plan":[]}'
            try:
                value = json_object_from_llm_text(raw_text)
            except Exception as parse_exc:
                try:
                    value = repair_json_object_from_llm_text(client, raw_text, schema_hint=schema_hint, max_tokens=1000)
                except Exception as repair_exc:
                    value = retry_json_object_with_compact_prompt(
                        client,
                        task_prompt=prompt,
                        schema_hint=schema_hint,
                        error=f"{type(parse_exc).__name__}: {parse_exc}; repair failed: {type(repair_exc).__name__}: {repair_exc}",
                        max_tokens=1200,
                    )
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
            client, label = self._client_for_agent(config, model, 0.0, agent)
            catalog = tool_catalog_prompt(agent, include_principles=True)
            allowed_tools = ", ".join(tool_names_for_agent(agent))
            prompt = (
                "你是当前 Agent 的下一步行动确认器。只确认下一步工具动作，不回答用户问题，不写最终答案。\n"
                "必须基于用户原话、会话摘要、上一步决策和当前上下文判断：现在是否应该执行 proposed_tool，目标是否说清楚，是否需要改用另一个允许工具。\n"
                f"当前 Agent 工具目录：\n{catalog}\n"
                f"允许工具名：{allowed_tools}, answer, evidence_select, final_answer_llm。\n"
                "如果 proposed_tool 合理，proceed=true；如果不合理，proceed=false 并给出 suggested_tool。不要拆 Crawler 的搜索词，不要替工具生成最终回答。\n"
                "CrawlerAgent 工具边界：temporary_extract 不保存本地文件；delegate_crawler 会启动后台采集循环。确认时必须检查 proposed_tool 的副作用是否符合用户对保存、入库、后台任务的要求。\n"
                "跨 Agent 沟通原则：确认的是消息是否应送达目标 Agent 或当前 Agent 是否应执行所选工具；不要在确认器里替接收方决定后续搜索、保存或最终回答。\n"
                "planned_workflow 完整性检查：如果 context.tool_decision.action_plan 没有覆盖用户明确要求的可执行副作用（例如采集、补充资料、保存、入库、交付给另一个 Agent、启动后台工作），proceed=false，并建议能完成该副作用的允许工具；如果 suggested_tool=planned_workflow，必须同时返回 action_plan，例如 mcagent_context 后 delegate_crawler。如果计划已经覆盖，则 proceed=true。\n"
                "只输出 JSON。\n"
                "额外工具 direct_answer：当用户只是问候、闲聊、询问系统能力、要求解释当前行为，或任何不需要本地资料/状态/Crawler 的问题时选择 direct_answer；选择它就不要触发 local_rag_search。\n"
                f"active_agent: {agent}\n"
                f"original_user_message: {original_question}\n"
                f"session_summary: {json.dumps(session_summary or {}, ensure_ascii=False)}\n"
                f"proposed_tool: {proposed_tool}\n"
                f"proposed_goal: {proposed_goal}\n"
                f"context: {json.dumps(context or {}, ensure_ascii=False)}\n"
                "JSON schema: {\"proceed\":true, \"tool\":\"工具名\", \"suggested_tool\":\"可选\", \"goal\":\"确认后的下一步目标\", \"reason\":\"一句理由\", \"concern\":\"可选风险\", \"action_plan\":[{\"step\":1,\"tool\":\"mcagent_context|delegate_crawler|...\",\"goal\":\"步骤目标\"}]}"
            )
            raw_text = client.chat(
                [
                    {"role": "system", "content": "只输出合法 JSON。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
                max_tokens=900,
            )
            schema_hint = '{"proceed":true,"tool":"tool name","suggested_tool":"","goal":"confirmed next step","reason":"why","concern":"","action_plan":[]}'
            try:
                value = json_object_from_llm_text(raw_text)
            except Exception as parse_exc:
                try:
                    value = repair_json_object_from_llm_text(client, raw_text, schema_hint=schema_hint, max_tokens=800)
                except Exception as repair_exc:
                    value = retry_json_object_with_compact_prompt(
                        client,
                        task_prompt=prompt,
                        schema_hint=schema_hint,
                        error=f"{type(parse_exc).__name__}: {parse_exc}; repair failed: {type(repair_exc).__name__}: {repair_exc}",
                        max_tokens=900,
                    )
            tool = str(value.get("tool") or proposed_tool).strip() or proposed_tool
            suggested = str(value.get("suggested_tool") or "").strip()
            return {
                "proceed": bool(value.get("proceed", True)),
                "tool": tool,
                "suggested_tool": suggested,
                "goal": str(value.get("goal") or proposed_goal).strip()[:500],
                "reason": str(value.get("reason") or "Agent confirmed next step.").strip()[:500],
                "concern": str(value.get("concern") or "").strip()[:500],
                "action_plan": value.get("action_plan") if isinstance(value.get("action_plan"), list) else [],
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
