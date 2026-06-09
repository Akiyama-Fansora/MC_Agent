from __future__ import annotations

from typing import Any, Callable

from langgraph.graph import END, START, StateGraph

from ..agent_runtime import domain_collection_tools_for_crawler, general_collection_tools_for_crawler
from ..config import AppConfig
from ..session_state import DEFAULT_SESSION_STORE
from .agent_state import AgentGraphState
from .graph_route_execution import (
    GRAPH_CRAWLER_AUDIT_ROUTE_EXECUTOR,
    GRAPH_STATUS_ROUTE_EXECUTOR,
    graph_crawler_audit_route_executor_metadata,
    graph_status_route_executor_metadata,
)
from .legacy_handler_surface_contract import build_legacy_handler_surface_contract
from .legacy_adapter import deliver_via_legacy_runtime
from .route_decision_output_contract import build_route_decision_output_contract
from .route_execution_contract import build_route_execution_contract
from .route_result_contract import build_route_result_contract
from .state import GraphEvent


EmitFn = Callable[[str, Any], None]
AgentDeliveryFn = Callable[..., dict[str, Any]]
AgentRouteDeciderFn = Callable[..., dict[str, Any]]
StatusExecutorFn = Callable[..., dict[str, Any]]
CrawlerAuditExecutorFn = Callable[..., dict[str, Any]]


def _event(node: str, status: str, detail: dict[str, Any] | None = None) -> GraphEvent:
    return {"node": node, "status": status, "detail": dict(detail or {})}


def _append(state: AgentGraphState, node: str, status: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "visited_nodes": [*state.get("visited_nodes", []), node],
        "graph_events": [*state.get("graph_events", []), _event(node, status, detail)],
    }


def build_crawler_graph(
    config: AppConfig,
    agent_delivery: AgentDeliveryFn,
    emit: EmitFn | None = None,
    *,
    route_decider: AgentRouteDeciderFn | None = None,
    status_executor: StatusExecutorFn | None = None,
    crawler_audit_executor: CrawlerAuditExecutorFn | None = None,
):
    builder = StateGraph(AgentGraphState)

    def receive(state: AgentGraphState) -> dict[str, Any]:
        payload = dict(state.get("payload") or {})
        payload["agent"] = "crawler_agent"
        general_tools = [tool.name for tool in general_collection_tools_for_crawler()]
        minecraft_tools = [tool.name for tool in domain_collection_tools_for_crawler("minecraft")]
        return {
            "agent_id": "crawler_agent",
            "payload": payload,
            "tool_boundary": {
                "agent": "CrawlerAgent",
                "allowed_capability_groups": [
                    "agent_message",
                    "web_discovery",
                    "fetch_url",
                    "browser_render",
                    "local_file_read",
                    "download",
                    "archive_extract",
                    "artifact_save",
                    "rag_ingest",
                    "optional_domain_toolsets",
                ],
                "general_collection_tools": general_tools,
                "domain_toolsets": {"minecraft": minecraft_tools},
                "principle": "Tools expose objective observations; CrawlerAgent LLM owns source graph, tool choice, observation review, retry/accept/reject, persistence, and final report.",
            },
            **_append(state, "crawler.receive", "message_received", {"agent": "crawler_agent"}),
        }

    def understand_boundary(state: AgentGraphState) -> dict[str, Any]:
        boundary = dict(state.get("tool_boundary") or {})
        thread_id = str(state.get("thread_id") or (state.get("payload") or {}).get("session_id") or "default")
        memory = DEFAULT_SESSION_STORE.context(thread_id, agent="crawler_agent").to_dict()
        return {
            "tool_boundary": boundary,
            "memory_context": memory,
            **_append(
                state,
                "crawler.understand_boundary",
                "general_domain_boundary_declared",
                {
                    "allowed_capability_groups": boundary.get("allowed_capability_groups", []),
                    "session_id": memory.get("session_id"),
                    "turn_count": memory.get("turn_count"),
                },
            ),
        }

    def select_tool_groups(state: AgentGraphState) -> dict[str, Any]:
        boundary = dict(state.get("tool_boundary") or {})
        selected = {
            "default_groups": ["general"],
            "default_tools": list(boundary.get("general_collection_tools") or []),
            "candidate_domain_toolsets": dict(boundary.get("domain_toolsets") or {}),
            "decision_owner": "CrawlerAgent LLM",
            "selection_contract": (
                "The graph exposes general tools by default and candidate domain toolsets as options. "
                "It does not decide semantic relevance or enable a domain plugin on the CrawlerAgent's behalf."
            ),
        }
        return {
            "selected_tool_groups": selected,
            **_append(
                state,
                "crawler.select_tool_groups",
                "default_general_candidates_exposed",
                {
                    "default_groups": selected["default_groups"],
                    "candidate_domain_toolsets": list(selected["candidate_domain_toolsets"].keys()),
                    "decision_owner": selected["decision_owner"],
                },
            ),
        }

    def prepare_mission_contract(state: AgentGraphState) -> dict[str, Any]:
        payload = dict(state.get("payload") or {})
        selected_groups = dict(state.get("selected_tool_groups") or {})
        message = payload.get("agent_message") if isinstance(payload.get("agent_message"), dict) else {}
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        contract = {
            "question": str(payload.get("question") or payload.get("query") or ""),
            "from_agent": str(message.get("from_agent") or payload.get("message_from") or ""),
            "delivery_target": str(metadata.get("delivery_target") or payload.get("delivery_target") or ""),
            "default_tools": list(selected_groups.get("default_tools") or []),
            "candidate_domain_toolsets": dict(selected_groups.get("candidate_domain_toolsets") or {}),
            "decision_owner": "CrawlerAgent LLM",
            "objective_contract": (
                "The graph records the mission facts and available tool groups. "
                "CrawlerAgent still owns source graph construction, domain choice, tool execution order, observation review, and persistence decisions."
            ),
        }
        return {
            "mission_contract": contract,
            **_append(
                state,
                "crawler.prepare_mission_contract",
                "mission_contract_exposed",
                {
                    "from_agent": contract["from_agent"],
                    "delivery_target": contract["delivery_target"],
                    "decision_owner": contract["decision_owner"],
                },
            ),
        }

    def prepare_source_planning_contract(state: AgentGraphState) -> dict[str, Any]:
        payload = dict(state.get("payload") or {})
        thread_id = str(state.get("thread_id") or payload.get("session_id") or "default")
        message = payload.get("agent_message") if isinstance(payload.get("agent_message"), dict) else {}
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        selected_groups = dict(state.get("selected_tool_groups") or {})
        mission = state.get("mission_contract") if isinstance(state.get("mission_contract"), dict) else {}
        memory = state.get("memory_context") if isinstance(state.get("memory_context"), dict) else {}
        summary = memory.get("summary") if isinstance(memory.get("summary"), dict) else {}
        preferred_hints = metadata.get("preferred_sources") if isinstance(metadata.get("preferred_sources"), list) else payload.get("preferred_sources")
        candidate_hints = metadata.get("candidate_sources") if isinstance(metadata.get("candidate_sources"), list) else payload.get("candidate_sources")
        preferred_hint_values = [str(item) for item in preferred_hints[:16]] if isinstance(preferred_hints, list) else []
        candidate_hint_records = [dict(item) for item in candidate_hints[:16] if isinstance(item, dict)] if isinstance(candidate_hints, list) else []
        planning_question = str(payload.get("question") or payload.get("query") or "")
        collection_target = str(
            metadata.get("collection_target")
            or metadata.get("task_goal")
            or payload.get("collection_target")
            or payload.get("task_goal")
            or planning_question
        )
        contract = {
            "contract_id": f"{thread_id}:crawler_agent:source_planning",
            "node": "crawler.prepare_source_planning_contract",
            "graph": "CrawlerAgentGraph",
            "agent_id": "crawler_agent",
            "session_id": str(payload.get("session_id") or thread_id),
            "contract_kind": "crawler_source_planning_input_contract",
            "planning_question": planning_question,
            "collection_target": collection_target,
            "delivery_target": str(mission.get("delivery_target") or metadata.get("delivery_target") or payload.get("delivery_target") or ""),
            "requested_by": str(metadata.get("requested_by") or payload.get("requested_by") or mission.get("from_agent") or ""),
            "source_dir": str(config.paths.source_dir),
            "source_dir_exists": config.paths.source_dir.exists(),
            "candidate_general_tools": list(selected_groups.get("default_tools") or []),
            "candidate_domain_toolsets": dict(selected_groups.get("candidate_domain_toolsets") or {}),
            "message_hints": {
                "preferred_source_hints": preferred_hint_values,
                "candidate_hint_records": candidate_hint_records,
                "candidate_hint_count": len(candidate_hint_records),
            },
            "session_context": {
                "turn_count": memory.get("turn_count") or 0,
                "summary_topics": [str(item) for item in (summary.get("topics") or [])[:12]],
                "summary_entities": [str(item) for item in (summary.get("entities") or [])[:12]],
            },
            "mission_contract": mission,
            "decision_owner": "CrawlerAgent LLM",
            "planner_still_runs_in_legacy_adapter": True,
            "objective_contract": (
                "The graph records source-planning inputs only. It does not choose sources, "
                "create tasks, build an action_plan, run tools, persist evidence, or judge observations."
            ),
        }
        return {
            "source_planning_contract": contract,
            **_append(
                state,
                "crawler.prepare_source_planning_contract",
                "source_planning_inputs_prepared",
                {
                    "contract_id": contract["contract_id"],
                    "contract_kind": contract["contract_kind"],
                    "candidate_general_tool_count": len(contract["candidate_general_tools"]),
                    "candidate_domain_toolsets": list(contract["candidate_domain_toolsets"].keys()),
                },
            ),
        }

    def prepare_message_preflight_contract(state: AgentGraphState) -> dict[str, Any]:
        payload = dict(state.get("payload") or {})
        thread_id = str(state.get("thread_id") or payload.get("session_id") or "default")
        message = payload.get("agent_message") if isinstance(payload.get("agent_message"), dict) else {}
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        intent = str(message.get("intent") or payload.get("intent") or "")
        metadata_tool = str(metadata.get("tool") or "")
        from_agent = str(message.get("from_agent") or payload.get("message_from") or "User")
        to_agent_id = str(message.get("to_agent_id") or payload.get("agent") or "crawler_agent")
        context_only = from_agent == "CrawlerAgent" and (intent == "mcagent_context_request" or metadata_tool == "mcagent_context")
        collection_request = to_agent_id == "crawler_agent" and (
            intent == "collection_request" or metadata_tool in {"collection_request", "delegate_crawler"}
        )
        contract = {
            "contract_id": f"{thread_id}:crawler_agent:message_preflight",
            "node": "crawler.prepare_message_preflight_contract",
            "graph": "CrawlerAgentGraph",
            "agent_id": "crawler_agent",
            "session_id": str(payload.get("session_id") or thread_id),
            "has_agent_message": bool(message),
            "message": {
                "message_id": str(message.get("message_id") or ""),
                "from_agent": from_agent,
                "from_agent_id": str(message.get("from_agent_id") or ""),
                "to_agent": str(message.get("to_agent") or "CrawlerAgent"),
                "to_agent_id": to_agent_id,
                "intent": intent,
                "metadata_tool": metadata_tool,
                "requires_reply": bool(message.get("requires_reply", True)),
            },
            "flags": {
                "context_only_agent_message": context_only,
                "collection_request_agent_message": collection_request,
                "message_only_cannot_execute_side_effect": True,
            },
            "decision_owner": "CrawlerAgent LLM",
            "objective_contract": (
                "The graph records message preflight facts only. It does not choose a crawler tool, "
                "start a background job, or persist evidence."
            ),
        }
        return {
            "message_preflight_contract": contract,
            **_append(
                state,
                "crawler.prepare_message_preflight_contract",
                "message_preflight_prepared",
                {
                    "contract_id": contract["contract_id"],
                    "context_only_agent_message": context_only,
                    "collection_request_agent_message": collection_request,
                },
            ),
        }

    def prepare_side_effect_authorization_contract(state: AgentGraphState) -> dict[str, Any]:
        payload = dict(state.get("payload") or {})
        thread_id = str(state.get("thread_id") or payload.get("session_id") or "default")
        message = payload.get("agent_message") if isinstance(payload.get("agent_message"), dict) else {}
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        message_preflight = state.get("message_preflight_contract") if isinstance(state.get("message_preflight_contract"), dict) else {}
        preflight_message = message_preflight.get("message") if isinstance(message_preflight.get("message"), dict) else {}
        flags = message_preflight.get("flags") if isinstance(message_preflight.get("flags"), dict) else {}
        metadata_tool = str(preflight_message.get("metadata_tool") or metadata.get("tool") or "")
        intent = str(preflight_message.get("intent") or message.get("intent") or payload.get("intent") or "")
        collection_request = bool(flags.get("collection_request_agent_message"))
        contract = {
            "contract_id": f"{thread_id}:crawler_agent:side_effect_authorization",
            "node": "crawler.prepare_side_effect_authorization_contract",
            "graph": "CrawlerAgentGraph",
            "agent_id": "crawler_agent",
            "session_id": str(payload.get("session_id") or thread_id),
            "contract_kind": "crawler_side_effect_authorization_facts_contract",
            "side_effect_surface": "start_background_job",
            "message_preflight_contract": message_preflight,
            "message_preflight_contract_id": message_preflight.get("contract_id") or "",
            "facts": {
                "has_agent_message": bool(message),
                "from_agent": str(preflight_message.get("from_agent") or message.get("from_agent") or payload.get("message_from") or "User"),
                "to_agent": str(preflight_message.get("to_agent") or message.get("to_agent") or "CrawlerAgent"),
                "to_agent_id": str(preflight_message.get("to_agent_id") or message.get("to_agent_id") or payload.get("agent") or "crawler_agent"),
                "intent": intent,
                "metadata_tool": metadata_tool,
                "collection_request_agent_message": collection_request,
                "metadata_mentions_delegate_crawler": metadata_tool == "delegate_crawler",
                "message_only_cannot_execute_side_effect": bool(flags.get("message_only_cannot_execute_side_effect", True)),
            },
            "required_agent_owned_decision": (
                "CrawlerAgent must later choose delegate_crawler, or planned_workflow with delegate_crawler in its action_plan, "
                "before the legacy runtime may start a background collection job."
            ),
            "authorization_evaluation_executed": False,
            "side_effect_executed": False,
            "legacy_guard_still_runs_in_adapter": True,
            "decision_owner": "CrawlerAgent LLM",
            "objective_contract": (
                "The graph records side-effect authorization facts only. It does not approve or deny execution, "
                "select delegate_crawler, create an action_plan, start a background job, or persist evidence."
            ),
        }
        return {
            "side_effect_authorization_contract": contract,
            **_append(
                state,
                "crawler.prepare_side_effect_authorization_contract",
                "side_effect_authorization_facts_prepared",
                {
                    "contract_id": contract["contract_id"],
                    "contract_kind": contract["contract_kind"],
                    "collection_request_agent_message": collection_request,
                    "side_effect_executed": contract["side_effect_executed"],
                },
            ),
        }

    def prepare_route_input_contract(state: AgentGraphState) -> dict[str, Any]:
        payload = dict(state.get("payload") or {})
        thread_id = str(state.get("thread_id") or payload.get("session_id") or "default")
        message = payload.get("agent_message") if isinstance(payload.get("agent_message"), dict) else {}
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        selected_groups = dict(state.get("selected_tool_groups") or {})
        message_preflight = state.get("message_preflight_contract") if isinstance(state.get("message_preflight_contract"), dict) else {}
        source_planning = state.get("source_planning_contract") if isinstance(state.get("source_planning_contract"), dict) else {}
        side_effect_authorization = state.get("side_effect_authorization_contract") if isinstance(state.get("side_effect_authorization_contract"), dict) else {}
        route_input_contract = {
            "contract_id": f"{thread_id}:crawler_agent:route_input",
            "node": "crawler.prepare_route_input_contract",
            "graph": "CrawlerAgentGraph",
            "agent_id": "crawler_agent",
            "session_id": str(payload.get("session_id") or thread_id),
            "contract_kind": "crawler_route_input_contract",
            "original_question": str(payload.get("question") or payload.get("query") or ""),
            "contextual_question_hint": str(payload.get("question") or payload.get("query") or ""),
            "message": {
                "from_agent": str(message.get("from_agent") or payload.get("message_from") or "User"),
                "to_agent": str(message.get("to_agent") or "CrawlerAgent"),
                "intent": str(message.get("intent") or ""),
                "delivery_target": str(metadata.get("delivery_target") or payload.get("delivery_target") or ""),
                "has_agent_message": bool(message),
            },
            "candidate_route_tools": list(selected_groups.get("default_tools") or []),
            "candidate_domain_toolsets": dict(selected_groups.get("candidate_domain_toolsets") or {}),
            "message_preflight_contract": message_preflight,
            "message_preflight_contract_id": message_preflight.get("contract_id") or "",
            "source_planning_contract": source_planning,
            "source_planning_contract_id": source_planning.get("contract_id") or "",
            "side_effect_authorization_contract": side_effect_authorization,
            "side_effect_authorization_contract_id": side_effect_authorization.get("contract_id") or "",
            "session_memory": state.get("memory_context") or {},
            "mission_contract": state.get("mission_contract") or {},
            "decision_owner": "CrawlerAgent LLM",
            "objective_contract": (
                "The graph prepares objective inputs for the later CrawlerAgent routing decision. "
                "It does not select a source, choose a tool, create a route_intent, or confirm side effects."
            ),
        }
        return {
            "route_input_contract": route_input_contract,
            **_append(
                state,
                "crawler.prepare_route_input_contract",
                "route_input_contract_prepared",
                {
                    "contract_id": route_input_contract["contract_id"],
                    "contract_kind": route_input_contract["contract_kind"],
                    "candidate_route_tool_count": len(route_input_contract["candidate_route_tools"]),
                    "decision_owner": route_input_contract["decision_owner"],
                },
            ),
        }

    def prepare_runtime_request(state: AgentGraphState) -> dict[str, Any]:
        payload = dict(state.get("payload") or {})
        thread_id = str(state.get("thread_id") or payload.get("session_id") or "default")
        message = payload.get("agent_message") if isinstance(payload.get("agent_message"), dict) else {}
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        route_input_contract = state.get("route_input_contract") if isinstance(state.get("route_input_contract"), dict) else {}
        message_preflight = state.get("message_preflight_contract") if isinstance(state.get("message_preflight_contract"), dict) else {}
        source_planning = state.get("source_planning_contract") if isinstance(state.get("source_planning_contract"), dict) else {}
        side_effect_authorization = state.get("side_effect_authorization_contract") if isinstance(state.get("side_effect_authorization_contract"), dict) else {}
        runtime_request = {
            "request_id": f"{thread_id}:crawler_agent:runtime_request",
            "node": "crawler.prepare_runtime_request",
            "graph": "CrawlerAgentGraph",
            "agent_id": "crawler_agent",
            "session_id": str(payload.get("session_id") or thread_id),
            "contract_kind": "crawler_collection_runtime_request",
            "payload": payload,
            "message": {
                "from_agent": str(message.get("from_agent") or payload.get("message_from") or "User"),
                "to_agent": str(message.get("to_agent") or "CrawlerAgent"),
                "intent": str(message.get("intent") or ""),
                "delivery_target": str(metadata.get("delivery_target") or payload.get("delivery_target") or ""),
                "has_agent_message": bool(message),
            },
            "tool_boundary": state.get("tool_boundary") or {},
            "selected_tool_groups": state.get("selected_tool_groups") or {},
            "memory_context": state.get("memory_context") or {},
            "mission_contract": state.get("mission_contract") or {},
            "source_planning_contract": source_planning,
            "source_planning_contract_id": source_planning.get("contract_id") or "",
            "message_preflight_contract": message_preflight,
            "message_preflight_contract_id": message_preflight.get("contract_id") or "",
            "side_effect_authorization_contract": side_effect_authorization,
            "side_effect_authorization_contract_id": side_effect_authorization.get("contract_id") or "",
            "route_input_contract": route_input_contract,
            "route_input_contract_id": route_input_contract.get("contract_id") or "",
            "decision_owner": "CrawlerAgent LLM",
            "objective_contract": (
                "The graph prepares objective runtime inputs for CrawlerAgent. It does not pick sources, "
                "enable a domain toolset, judge observations, persist evidence, or write the final report."
            ),
        }
        return {
            "runtime_request": runtime_request,
            **_append(
                state,
                "crawler.prepare_runtime_request",
                "runtime_request_prepared",
                {
                    "request_id": runtime_request["request_id"],
                    "contract_kind": runtime_request["contract_kind"],
                    "decision_owner": runtime_request["decision_owner"],
                },
            ),
        }

    def run_legacy_adapter(state: AgentGraphState) -> dict[str, Any]:
        runtime_request = state.get("runtime_request") if isinstance(state.get("runtime_request"), dict) else {}
        route_decision = state.get("route_decision") if isinstance(state.get("route_decision"), dict) else {}
        result = deliver_via_legacy_runtime(
            config,
            dict(state.get("payload") or {}),
            agent_delivery=agent_delivery,
            emit=emit,
            agent_id="crawler_agent",
            graph_name="CrawlerAgentGraph",
            node_name="crawler.legacy_adapter",
            runtime_request=runtime_request,
            route_decision=route_decision,
        )
        return {
            "result": result,
            "runtime_adapter": result.get("legacy_runtime_adapter") or {},
            **_append(
                state,
                "crawler.legacy_adapter",
                "delegated_to_legacy_runtime_adapter",
                {
                    "agent": "crawler_agent",
                    "adapter": "legacy_web_server_runtime",
                    "route_decision_id": route_decision.get("route_decision_id") or "",
                    "route_intent": route_decision.get("route_intent") or "",
                },
            ),
        }

    def route_agent_decision(state: AgentGraphState) -> dict[str, Any]:
        payload = dict(state.get("payload") or {})
        runtime_request = state.get("runtime_request") if isinstance(state.get("runtime_request"), dict) else {}
        thread_id = str(state.get("thread_id") or payload.get("session_id") or "default")
        if route_decider is None:
            decision = {
                "route_decision_id": f"{thread_id}:crawler_agent:route_decision",
                "node": "crawler.route_agent_decision",
                "graph": "CrawlerAgentGraph",
                "agent_id": "crawler_agent",
                "session_id": str(payload.get("session_id") or thread_id),
                "routed": False,
                "route_intent": "",
                "decision_owner": "CrawlerAgent LLM",
                "legacy_fallback_required": True,
                "reason": "No graph route decider was injected; this graph run stays on the legacy adapter path.",
                "objective_contract": (
                    "The graph may invoke the existing Agent router only when a route decider is injected. "
                    "It does not infer tools from message text or AgentMessage metadata."
                ),
            }
        else:
            try:
                decision = dict(
                    route_decider(
                        config,
                        payload,
                        agent_id="crawler_agent",
                        graph_name="CrawlerAgentGraph",
                        node_name="crawler.route_agent_decision",
                        runtime_request=runtime_request,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - routing failures must fall back without executing tools.
                decision = {
                    "routed": False,
                    "route_intent": "router_error",
                    "error": f"{type(exc).__name__}: {exc}",
                    "legacy_fallback_required": True,
                }
            decision.setdefault("route_decision_id", f"{thread_id}:crawler_agent:route_decision")
            decision.setdefault("node", "crawler.route_agent_decision")
            decision.setdefault("graph", "CrawlerAgentGraph")
            decision.setdefault("agent_id", "crawler_agent")
            decision.setdefault("session_id", str(payload.get("session_id") or thread_id))
            decision.setdefault("decision_owner", "CrawlerAgent LLM")
            decision.setdefault(
                "objective_contract",
                "The graph invoked the existing Agent router to obtain CrawlerAgent's tool decision. It did not use keyword routing or alter AgentMessage delivery.",
            )
        return {
            "route_decision": decision,
            **_append(
                state,
                "crawler.route_agent_decision",
                "agent_route_decision_recorded" if decision.get("routed") else "agent_route_decision_deferred",
                {
                    "route_decision_id": decision.get("route_decision_id") or "",
                    "route_intent": decision.get("route_intent") or "",
                    "decision_owner": decision.get("decision_owner") or "CrawlerAgent LLM",
                    "legacy_fallback_required": bool(decision.get("legacy_fallback_required")),
                },
            ),
        }

    def run_graph_status_route(state: AgentGraphState) -> dict[str, Any]:
        runtime_request = state.get("runtime_request") if isinstance(state.get("runtime_request"), dict) else {}
        route_decision = state.get("route_decision") if isinstance(state.get("route_decision"), dict) else {}
        result = dict(
            status_executor(
                config,
                dict(state.get("payload") or {}),
                emit=emit,
                agent_id="crawler_agent",
                graph_name="CrawlerAgentGraph",
                node_name="crawler.graph_status_route",
                runtime_request=runtime_request,
                route_decision=route_decision,
            )
        )
        adapter = graph_status_route_executor_metadata(
            agent_id="crawler_agent",
            graph_name="CrawlerAgentGraph",
            node_name="crawler.graph_status_route",
            runtime_request=runtime_request,
            route_decision=route_decision,
        )
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        result["metadata"] = {**metadata, "graph_route_executor": adapter}
        result["graph_route_executor"] = adapter
        return {
            "result": result,
            "runtime_adapter": adapter,
            **_append(
                state,
                "crawler.graph_status_route",
                "executed_agent_selected_status",
                {
                    "agent": "crawler_agent",
                    "adapter": GRAPH_STATUS_ROUTE_EXECUTOR,
                    "route_decision_id": route_decision.get("route_decision_id") or "",
                },
            ),
        }

    def run_graph_crawler_audit_route(state: AgentGraphState) -> dict[str, Any]:
        runtime_request = state.get("runtime_request") if isinstance(state.get("runtime_request"), dict) else {}
        route_decision = state.get("route_decision") if isinstance(state.get("route_decision"), dict) else {}
        result = dict(
            crawler_audit_executor(
                config,
                dict(state.get("payload") or {}),
                emit=emit,
                agent_id="crawler_agent",
                graph_name="CrawlerAgentGraph",
                node_name="crawler.graph_crawler_audit_route",
                runtime_request=runtime_request,
                route_decision=route_decision,
            )
        )
        adapter = graph_crawler_audit_route_executor_metadata(
            agent_id="crawler_agent",
            graph_name="CrawlerAgentGraph",
            node_name="crawler.graph_crawler_audit_route",
            runtime_request=runtime_request,
            route_decision=route_decision,
        )
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        result["metadata"] = {**metadata, "graph_route_executor": adapter}
        result["graph_route_executor"] = adapter
        return {
            "result": result,
            "runtime_adapter": adapter,
            **_append(
                state,
                "crawler.graph_crawler_audit_route",
                "executed_agent_selected_crawler_audit",
                {
                    "agent": "crawler_agent",
                    "adapter": GRAPH_CRAWLER_AUDIT_ROUTE_EXECUTOR,
                    "route_decision_id": route_decision.get("route_decision_id") or "",
                },
            ),
        }

    def prepare_route_result_contract(state: AgentGraphState) -> dict[str, Any]:
        result = dict(state.get("result") or {})
        runtime_request = state.get("runtime_request") if isinstance(state.get("runtime_request"), dict) else {}
        runtime_adapter = state.get("runtime_adapter") if isinstance(state.get("runtime_adapter"), dict) else {}
        route_input = state.get("route_input_contract") if isinstance(state.get("route_input_contract"), dict) else {}
        message_preflight = state.get("message_preflight_contract") if isinstance(state.get("message_preflight_contract"), dict) else {}
        source_planning = state.get("source_planning_contract") if isinstance(state.get("source_planning_contract"), dict) else {}
        side_effect_authorization = state.get("side_effect_authorization_contract") if isinstance(state.get("side_effect_authorization_contract"), dict) else {}
        route_decision_output = state.get("route_decision_output_contract") if isinstance(state.get("route_decision_output_contract"), dict) else {}
        route_execution = state.get("route_execution_contract") if isinstance(state.get("route_execution_contract"), dict) else {}
        legacy_handler_surface = state.get("legacy_handler_surface_contract") if isinstance(state.get("legacy_handler_surface_contract"), dict) else {}
        thread_id = str(state.get("thread_id") or runtime_request.get("session_id") or result.get("session_id") or "default")
        contract = build_route_result_contract(
            thread_id=thread_id,
            graph_name="CrawlerAgentGraph",
            agent_id="crawler_agent",
            node_name="crawler.prepare_route_result_contract",
            contract_kind="crawler_route_result_contract",
            decision_owner="CrawlerAgent LLM",
            result=result,
            runtime_request=runtime_request,
            runtime_adapter=runtime_adapter,
            route_input_contract=route_input,
            message_preflight_contract=message_preflight,
            source_planning_contract=source_planning,
            side_effect_authorization_contract=side_effect_authorization,
            route_decision_output_contract=route_decision_output,
            route_execution_contract=route_execution,
            legacy_handler_surface_contract=legacy_handler_surface,
        )
        shape = contract["result_shape"]
        return {
            "route_result_contract": contract,
            **_append(
                state,
                "crawler.prepare_route_result_contract",
                "route_result_shape_recorded",
                {
                    "contract_id": contract["contract_id"],
                    "contract_kind": contract["contract_kind"],
                    "answer_present": shape["answer_present"],
                    "source_count": shape["source_count"],
                    "job_id_present": shape["job_id_present"],
                },
            ),
        }

    def prepare_legacy_handler_surface_contract(state: AgentGraphState) -> dict[str, Any]:
        runtime_request = state.get("runtime_request") if isinstance(state.get("runtime_request"), dict) else {}
        runtime_adapter = state.get("runtime_adapter") if isinstance(state.get("runtime_adapter"), dict) else {}
        route_decision_output = state.get("route_decision_output_contract") if isinstance(state.get("route_decision_output_contract"), dict) else {}
        route_execution = state.get("route_execution_contract") if isinstance(state.get("route_execution_contract"), dict) else {}
        thread_id = str(state.get("thread_id") or runtime_request.get("session_id") or "default")
        contract = build_legacy_handler_surface_contract(
            thread_id=thread_id,
            graph_name="CrawlerAgentGraph",
            agent_id="crawler_agent",
            node_name="crawler.prepare_legacy_handler_surface_contract",
            contract_kind="crawler_legacy_handler_surface_facts_contract",
            decision_owner="CrawlerAgent LLM",
            runtime_request=runtime_request,
            runtime_adapter=runtime_adapter,
            route_decision_output_contract=route_decision_output,
            route_execution_contract=route_execution,
        )
        return {
            "legacy_handler_surface_contract": contract,
            **_append(
                state,
                "crawler.prepare_legacy_handler_surface_contract",
                "legacy_handler_surface_facts_recorded",
                {
                    "contract_id": contract["contract_id"],
                    "contract_kind": contract["contract_kind"],
                    "candidate_surface_count": contract["candidate_surface_count"],
                    "observed_surface_signal_count": contract["observed_surface_signal_count"],
                },
            ),
        }

    def prepare_route_execution_contract(state: AgentGraphState) -> dict[str, Any]:
        result = dict(state.get("result") or {})
        runtime_request = state.get("runtime_request") if isinstance(state.get("runtime_request"), dict) else {}
        runtime_adapter = state.get("runtime_adapter") if isinstance(state.get("runtime_adapter"), dict) else {}
        route_input = state.get("route_input_contract") if isinstance(state.get("route_input_contract"), dict) else {}
        route_decision_output = state.get("route_decision_output_contract") if isinstance(state.get("route_decision_output_contract"), dict) else {}
        message_preflight = state.get("message_preflight_contract") if isinstance(state.get("message_preflight_contract"), dict) else {}
        source_planning = state.get("source_planning_contract") if isinstance(state.get("source_planning_contract"), dict) else {}
        side_effect_authorization = state.get("side_effect_authorization_contract") if isinstance(state.get("side_effect_authorization_contract"), dict) else {}
        thread_id = str(state.get("thread_id") or runtime_request.get("session_id") or result.get("session_id") or "default")
        contract = build_route_execution_contract(
            thread_id=thread_id,
            graph_name="CrawlerAgentGraph",
            agent_id="crawler_agent",
            node_name="crawler.prepare_route_execution_contract",
            contract_kind="crawler_route_execution_facts_contract",
            decision_owner="CrawlerAgent LLM",
            result=result,
            runtime_request=runtime_request,
            runtime_adapter=runtime_adapter,
            route_input_contract=route_input,
            route_decision_output_contract=route_decision_output,
            message_preflight_contract=message_preflight,
            source_planning_contract=source_planning,
            side_effect_authorization_contract=side_effect_authorization,
        )
        trace_facts = contract["trace_facts"]
        result_facts = contract["result_facts"]
        return {
            "route_execution_contract": contract,
            **_append(
                state,
                "crawler.prepare_route_execution_contract",
                "route_execution_facts_recorded",
                {
                    "contract_id": contract["contract_id"],
                    "contract_kind": contract["contract_kind"],
                    "observed_execution_stages": trace_facts["observed_execution_stages"],
                    "answer_present": result_facts["answer_present"],
                    "job_present": result_facts["job_present"],
                    "has_delegate_trace": trace_facts["has_delegate_trace"],
                },
            ),
        }

    def prepare_route_decision_output_contract(state: AgentGraphState) -> dict[str, Any]:
        result = dict(state.get("result") or {})
        runtime_request = state.get("runtime_request") if isinstance(state.get("runtime_request"), dict) else {}
        runtime_adapter = state.get("runtime_adapter") if isinstance(state.get("runtime_adapter"), dict) else {}
        route_input = state.get("route_input_contract") if isinstance(state.get("route_input_contract"), dict) else {}
        message_preflight = state.get("message_preflight_contract") if isinstance(state.get("message_preflight_contract"), dict) else {}
        source_planning = state.get("source_planning_contract") if isinstance(state.get("source_planning_contract"), dict) else {}
        side_effect_authorization = state.get("side_effect_authorization_contract") if isinstance(state.get("side_effect_authorization_contract"), dict) else {}
        thread_id = str(state.get("thread_id") or runtime_request.get("session_id") or result.get("session_id") or "default")
        contract = build_route_decision_output_contract(
            thread_id=thread_id,
            graph_name="CrawlerAgentGraph",
            agent_id="crawler_agent",
            node_name="crawler.prepare_route_decision_output_contract",
            contract_kind="crawler_route_decision_output_facts_contract",
            decision_owner="CrawlerAgent LLM",
            result=result,
            runtime_request=runtime_request,
            runtime_adapter=runtime_adapter,
            route_input_contract=route_input,
            message_preflight_contract=message_preflight,
            source_planning_contract=source_planning,
            side_effect_authorization_contract=side_effect_authorization,
        )
        facts = contract["trace_facts"]
        return {
            "route_decision_output_contract": contract,
            **_append(
                state,
                "crawler.prepare_route_decision_output_contract",
                "route_decision_output_facts_recorded",
                {
                    "contract_id": contract["contract_id"],
                    "contract_kind": contract["contract_kind"],
                    "has_tool_selected_trace": facts["has_tool_selected_trace"],
                    "observed_selected_tool": facts["observed_selected_tool"],
                    "result_job_id_present": facts["result_job_id_present"],
                },
            ),
        }

    def finalize(state: AgentGraphState) -> dict[str, Any]:
        result = dict(state.get("result") or {})
        agent_runtime = {
            "agent_graph": "CrawlerAgentGraph",
            "agent_id": "crawler_agent",
            "tool_boundary": state.get("tool_boundary") or {},
            "selected_tool_groups": state.get("selected_tool_groups") or {},
            "memory_context": state.get("memory_context") or {},
            "mission_contract": state.get("mission_contract") or {},
            "source_planning_contract": state.get("source_planning_contract") or {},
            "message_preflight_contract": state.get("message_preflight_contract") or {},
            "side_effect_authorization_contract": state.get("side_effect_authorization_contract") or {},
            "route_input_contract": state.get("route_input_contract") or {},
            "runtime_request": state.get("runtime_request") or {},
            "route_decision": state.get("route_decision") or {},
            "runtime_adapter": state.get("runtime_adapter") or result.get("legacy_runtime_adapter") or {},
            "route_decision_output_contract": state.get("route_decision_output_contract") or {},
            "route_execution_contract": state.get("route_execution_contract") or {},
            "legacy_handler_surface_contract": state.get("legacy_handler_surface_contract") or {},
            "route_result_contract": state.get("route_result_contract") or {},
            "visited_nodes": [*state.get("visited_nodes", []), "crawler.finalize"],
            "events": [*state.get("graph_events", []), _event("crawler.finalize", "ready")],
        }
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        metadata = {**metadata, "agent_graph_runtime": agent_runtime}
        result["metadata"] = metadata
        result["agent_graph_runtime"] = agent_runtime
        return {
            "result": result,
            "visited_nodes": agent_runtime["visited_nodes"],
            "graph_events": agent_runtime["events"],
        }

    builder.add_node("receive", receive)
    builder.add_node("understand_boundary", understand_boundary)
    builder.add_node("select_tool_groups", select_tool_groups)
    builder.add_node("prepare_mission_contract", prepare_mission_contract)
    builder.add_node("prepare_source_planning_contract", prepare_source_planning_contract)
    builder.add_node("prepare_message_preflight_contract", prepare_message_preflight_contract)
    builder.add_node("prepare_side_effect_authorization_contract", prepare_side_effect_authorization_contract)
    builder.add_node("prepare_route_input_contract", prepare_route_input_contract)
    builder.add_node("prepare_runtime_request", prepare_runtime_request)
    builder.add_node("route_agent_decision", route_agent_decision)
    builder.add_node("legacy_adapter", run_legacy_adapter)
    builder.add_node("graph_status_route", run_graph_status_route)
    builder.add_node("graph_crawler_audit_route", run_graph_crawler_audit_route)
    builder.add_node("prepare_route_decision_output_contract", prepare_route_decision_output_contract)
    builder.add_node("prepare_route_execution_contract", prepare_route_execution_contract)
    builder.add_node("prepare_legacy_handler_surface_contract", prepare_legacy_handler_surface_contract)
    builder.add_node("prepare_route_result_contract", prepare_route_result_contract)
    builder.add_node("finalize", finalize)
    builder.add_edge(START, "receive")
    builder.add_edge("receive", "understand_boundary")
    builder.add_edge("understand_boundary", "select_tool_groups")
    builder.add_edge("select_tool_groups", "prepare_mission_contract")
    builder.add_edge("prepare_mission_contract", "prepare_source_planning_contract")
    builder.add_edge("prepare_source_planning_contract", "prepare_message_preflight_contract")
    builder.add_edge("prepare_message_preflight_contract", "prepare_side_effect_authorization_contract")
    builder.add_edge("prepare_side_effect_authorization_contract", "prepare_route_input_contract")
    builder.add_edge("prepare_route_input_contract", "prepare_runtime_request")
    builder.add_edge("prepare_runtime_request", "route_agent_decision")

    def route_execution_key(state: AgentGraphState) -> str:
        decision = state.get("route_decision") if isinstance(state.get("route_decision"), dict) else {}
        if status_executor is not None and decision.get("routed") and decision.get("route_intent") == "status":
            return "status"
        if crawler_audit_executor is not None and decision.get("routed") and decision.get("route_intent") == "crawler_audit":
            return "crawler_audit"
        return "legacy"

    builder.add_conditional_edges(
        "route_agent_decision",
        route_execution_key,
        {"status": "graph_status_route", "crawler_audit": "graph_crawler_audit_route", "legacy": "legacy_adapter"},
    )
    builder.add_edge("graph_status_route", "prepare_route_decision_output_contract")
    builder.add_edge("graph_crawler_audit_route", "prepare_route_decision_output_contract")
    builder.add_edge("legacy_adapter", "prepare_route_decision_output_contract")
    builder.add_edge("prepare_route_decision_output_contract", "prepare_route_execution_contract")
    builder.add_edge("prepare_route_execution_contract", "prepare_legacy_handler_surface_contract")
    builder.add_edge("prepare_legacy_handler_surface_contract", "prepare_route_result_contract")
    builder.add_edge("prepare_route_result_contract", "finalize")
    builder.add_edge("finalize", END)
    return builder.compile()


def run_crawler_graph(
    config: AppConfig,
    payload: dict[str, Any],
    *,
    agent_delivery: AgentDeliveryFn,
    emit: EmitFn | None = None,
    thread_id: str = "default",
    route_decider: AgentRouteDeciderFn | None = None,
    status_executor: StatusExecutorFn | None = None,
    crawler_audit_executor: CrawlerAuditExecutorFn | None = None,
) -> dict[str, Any]:
    graph = build_crawler_graph(
        config,
        agent_delivery,
        emit=emit,
        route_decider=route_decider,
        status_executor=status_executor,
        crawler_audit_executor=crawler_audit_executor,
    )
    final_state = graph.invoke(
        {
            "thread_id": thread_id,
            "agent_id": "crawler_agent",
            "payload": dict(payload),
            "tool_boundary": {},
            "graph_events": [],
            "visited_nodes": [],
            "errors": [],
        }
    )
    return dict(final_state.get("result") or {})
