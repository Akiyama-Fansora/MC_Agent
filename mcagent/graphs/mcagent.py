from __future__ import annotations

from typing import Any, Callable

from langgraph.graph import END, START, StateGraph

from ..agent_runtime import tools_for_agent
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


def build_mcagent_graph(
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
        payload["agent"] = "mcagent_rag"
        local_tools = [tool.name for tool in tools_for_agent("mcagent_rag")]
        return {
            "agent_id": "mcagent_rag",
            "payload": payload,
            "tool_boundary": {
                "agent": "MCagent",
                "allowed_capability_groups": ["local_rag", "local_session_memory", "agent_message"],
                "blocked_capability_groups": ["web_search", "browser", "download", "archive_extract", "public_web_ingest"],
                "local_tools": local_tools,
                "principle": "MCagent answers only from local data/RAG and may request CrawlerAgent through AgentMessage when local evidence is insufficient.",
            },
            **_append(state, "mcagent.receive", "message_received", {"agent": "mcagent_rag"}),
        }

    def load_memory_boundary(state: AgentGraphState) -> dict[str, Any]:
        boundary = dict(state.get("tool_boundary") or {})
        thread_id = str(state.get("thread_id") or (state.get("payload") or {}).get("session_id") or "default")
        memory = DEFAULT_SESSION_STORE.context(thread_id, agent="mcagent_rag").to_dict()
        return {
            "tool_boundary": boundary,
            "memory_context": memory,
            **_append(
                state,
                "mcagent.load_memory_boundary",
                "local_only_boundary_declared",
                {
                    "allowed_capability_groups": boundary.get("allowed_capability_groups", []),
                    "session_id": memory.get("session_id"),
                    "turn_count": memory.get("turn_count"),
                },
            ),
        }

    def select_local_tools(state: AgentGraphState) -> dict[str, Any]:
        boundary = dict(state.get("tool_boundary") or {})
        selected = {
            "default_groups": ["local"],
            "default_tools": list(boundary.get("local_tools") or []),
            "blocked_groups": list(boundary.get("blocked_capability_groups") or []),
            "decision_owner": "MCagent LLM",
            "selection_contract": (
                "MCagent may choose only local/RAG/status/message tools. "
                "Network, browser, download, archive extraction, and public web ingest tools are not registered to this graph."
            ),
        }
        return {
            "selected_tool_groups": selected,
            **_append(
                state,
                "mcagent.select_local_tools",
                "local_tools_exposed",
                {
                    "default_groups": selected["default_groups"],
                    "blocked_groups": selected["blocked_groups"],
                    "decision_owner": selected["decision_owner"],
                },
            ),
        }

    def prepare_local_retrieval(state: AgentGraphState) -> dict[str, Any]:
        payload = dict(state.get("payload") or {})
        contract = {
            "question": str(payload.get("question") or payload.get("query") or ""),
            "allowed_evidence_sources": ["local_rag", "local_corpus_inventory", "session_memory"],
            "blocked_evidence_sources": ["public_web", "browser", "downloaded_archive_without_crawler"],
            "decision_owner": "MCagent LLM",
            "objective_contract": (
                "The graph exposes the local evidence boundary. Retrieval candidates and evidence sufficiency "
                "must still be judged by MCagent's LLM or the legacy MCagent runtime during migration."
            ),
        }
        return {
            "retrieval_contract": contract,
            **_append(
                state,
                "mcagent.prepare_local_retrieval",
                "local_evidence_contract_exposed",
                {
                    "allowed_evidence_sources": contract["allowed_evidence_sources"],
                    "blocked_evidence_sources": contract["blocked_evidence_sources"],
                    "decision_owner": contract["decision_owner"],
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
        to_agent_id = str(message.get("to_agent_id") or payload.get("agent") or "mcagent_rag")
        context_only = from_agent == "CrawlerAgent" and (intent == "mcagent_context_request" or metadata_tool == "mcagent_context")
        collection_request = to_agent_id == "crawler_agent" and (
            intent == "collection_request" or metadata_tool in {"collection_request", "delegate_crawler"}
        )
        contract = {
            "contract_id": f"{thread_id}:mcagent_rag:message_preflight",
            "node": "mcagent.prepare_message_preflight_contract",
            "graph": "MCagentGraph",
            "agent_id": "mcagent_rag",
            "session_id": str(payload.get("session_id") or thread_id),
            "has_agent_message": bool(message),
            "message": {
                "message_id": str(message.get("message_id") or ""),
                "from_agent": from_agent,
                "from_agent_id": str(message.get("from_agent_id") or ""),
                "to_agent": str(message.get("to_agent") or "MCagent"),
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
            "decision_owner": "MCagent LLM",
            "objective_contract": (
                "The graph records message preflight facts only. It does not reply to context requests, "
                "select tools, or execute side effects."
            ),
        }
        return {
            "message_preflight_contract": contract,
            **_append(
                state,
                "mcagent.prepare_message_preflight_contract",
                "message_preflight_prepared",
                {
                    "contract_id": contract["contract_id"],
                    "context_only_agent_message": context_only,
                    "collection_request_agent_message": collection_request,
                },
            ),
        }

    def prepare_contextual_question_contract(state: AgentGraphState) -> dict[str, Any]:
        payload = dict(state.get("payload") or {})
        thread_id = str(state.get("thread_id") or payload.get("session_id") or "default")
        question = str(payload.get("question") or payload.get("query") or "")
        memory = state.get("memory_context") if isinstance(state.get("memory_context"), dict) else {}
        history = memory.get("history") if isinstance(memory.get("history"), list) else []
        summary = memory.get("summary") if isinstance(memory.get("summary"), dict) else {}
        recent_questions = [str(item.get("question") or "") for item in history[-5:] if isinstance(item, dict) and item.get("question")]
        context_terms = [
            *[str(item) for item in (summary.get("topics") or [])[:12]],
            *[str(item) for item in (summary.get("names") or [])[:12]],
            *[str(item) for item in (summary.get("entities") or [])[:12]],
        ]
        contract = {
            "contract_id": f"{thread_id}:mcagent_rag:contextual_question",
            "node": "mcagent.prepare_contextual_question_contract",
            "graph": "MCagentGraph",
            "agent_id": "mcagent_rag",
            "session_id": str(payload.get("session_id") or thread_id),
            "contract_kind": "mcagent_contextual_question_contract",
            "original_question": question,
            "contextual_question_hint": question,
            "history_turn_count": len(history),
            "recent_questions": recent_questions,
            "summary_topics": [str(item) for item in (summary.get("topics") or [])[:12]],
            "summary_names": [str(item) for item in (summary.get("names") or [])[:12]],
            "summary_entities": [str(item) for item in (summary.get("entities") or [])[:12]],
            "candidate_context_terms": [item for item in context_terms if item][:24],
            "context_inputs_available": bool(history or summary),
            "rewrite_executed": False,
            "legacy_contextualize_still_runs_in_adapter": True,
            "decision_owner": "MCagent LLM",
            "objective_contract": (
                "The graph records contextual-question inputs only. It does not rewrite the question, "
                "select a route, judge evidence, or change the payload handed to the legacy adapter."
            ),
        }
        return {
            "contextual_question_contract": contract,
            **_append(
                state,
                "mcagent.prepare_contextual_question_contract",
                "contextual_question_inputs_prepared",
                {
                    "contract_id": contract["contract_id"],
                    "history_turn_count": contract["history_turn_count"],
                    "context_inputs_available": contract["context_inputs_available"],
                    "rewrite_executed": contract["rewrite_executed"],
                },
            ),
        }

    def prepare_route_input_contract(state: AgentGraphState) -> dict[str, Any]:
        payload = dict(state.get("payload") or {})
        thread_id = str(state.get("thread_id") or payload.get("session_id") or "default")
        message = payload.get("agent_message") if isinstance(payload.get("agent_message"), dict) else {}
        selected_groups = dict(state.get("selected_tool_groups") or {})
        message_preflight = state.get("message_preflight_contract") if isinstance(state.get("message_preflight_contract"), dict) else {}
        contextual_question = state.get("contextual_question_contract") if isinstance(state.get("contextual_question_contract"), dict) else {}
        route_input_contract = {
            "contract_id": f"{thread_id}:mcagent_rag:route_input",
            "node": "mcagent.prepare_route_input_contract",
            "graph": "MCagentGraph",
            "agent_id": "mcagent_rag",
            "session_id": str(payload.get("session_id") or thread_id),
            "contract_kind": "mcagent_route_input_contract",
            "original_question": str(payload.get("question") or payload.get("query") or ""),
            "contextual_question_hint": str(contextual_question.get("contextual_question_hint") or payload.get("question") or payload.get("query") or ""),
            "message": {
                "from_agent": str(message.get("from_agent") or payload.get("message_from") or "User"),
                "to_agent": str(message.get("to_agent") or "MCagent"),
                "intent": str(message.get("intent") or ""),
                "has_agent_message": bool(message),
            },
            "candidate_route_tools": list(selected_groups.get("default_tools") or []),
            "blocked_capability_groups": list((state.get("tool_boundary") or {}).get("blocked_capability_groups") or []),
            "message_preflight_contract": message_preflight,
            "message_preflight_contract_id": message_preflight.get("contract_id") or "",
            "contextual_question_contract": contextual_question,
            "contextual_question_contract_id": contextual_question.get("contract_id") or "",
            "session_memory": state.get("memory_context") or {},
            "retrieval_contract": state.get("retrieval_contract") or {},
            "decision_owner": "MCagent LLM",
            "objective_contract": (
                "The graph prepares objective inputs for the later tool-routing decision. "
                "It does not select a tool, create a route_intent, or confirm side effects."
            ),
        }
        return {
            "route_input_contract": route_input_contract,
            **_append(
                state,
                "mcagent.prepare_route_input_contract",
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
        route_input_contract = state.get("route_input_contract") if isinstance(state.get("route_input_contract"), dict) else {}
        message_preflight = state.get("message_preflight_contract") if isinstance(state.get("message_preflight_contract"), dict) else {}
        contextual_question = state.get("contextual_question_contract") if isinstance(state.get("contextual_question_contract"), dict) else {}
        runtime_request = {
            "request_id": f"{thread_id}:mcagent_rag:runtime_request",
            "node": "mcagent.prepare_runtime_request",
            "graph": "MCagentGraph",
            "agent_id": "mcagent_rag",
            "session_id": str(payload.get("session_id") or thread_id),
            "contract_kind": "mcagent_local_runtime_request",
            "payload": payload,
            "message": {
                "from_agent": str(message.get("from_agent") or payload.get("message_from") or "User"),
                "to_agent": str(message.get("to_agent") or "MCagent"),
                "intent": str(message.get("intent") or ""),
                "has_agent_message": bool(message),
            },
            "tool_boundary": state.get("tool_boundary") or {},
            "selected_tool_groups": state.get("selected_tool_groups") or {},
            "memory_context": state.get("memory_context") or {},
            "retrieval_contract": state.get("retrieval_contract") or {},
            "message_preflight_contract": message_preflight,
            "message_preflight_contract_id": message_preflight.get("contract_id") or "",
            "contextual_question_contract": contextual_question,
            "contextual_question_contract_id": contextual_question.get("contract_id") or "",
            "route_input_contract": route_input_contract,
            "route_input_contract_id": route_input_contract.get("contract_id") or "",
            "decision_owner": "MCagent LLM",
            "objective_contract": (
                "The graph prepares objective runtime inputs for MCagent. It does not choose the final tool, "
                "judge evidence sufficiency, or write the natural-language answer."
            ),
        }
        return {
            "runtime_request": runtime_request,
            **_append(
                state,
                "mcagent.prepare_runtime_request",
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
            agent_id="mcagent_rag",
            graph_name="MCagentGraph",
            node_name="mcagent.legacy_adapter",
            runtime_request=runtime_request,
            route_decision=route_decision,
        )
        return {
            "result": result,
            "runtime_adapter": result.get("legacy_runtime_adapter") or {},
            **_append(
                state,
                "mcagent.legacy_adapter",
                "delegated_to_legacy_runtime_adapter",
                {
                    "agent": "mcagent_rag",
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
                "route_decision_id": f"{thread_id}:mcagent_rag:route_decision",
                "node": "mcagent.route_agent_decision",
                "graph": "MCagentGraph",
                "agent_id": "mcagent_rag",
                "session_id": str(payload.get("session_id") or thread_id),
                "routed": False,
                "route_intent": "",
                "decision_owner": "MCagent LLM",
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
                        agent_id="mcagent_rag",
                        graph_name="MCagentGraph",
                        node_name="mcagent.route_agent_decision",
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
            decision.setdefault("route_decision_id", f"{thread_id}:mcagent_rag:route_decision")
            decision.setdefault("node", "mcagent.route_agent_decision")
            decision.setdefault("graph", "MCagentGraph")
            decision.setdefault("agent_id", "mcagent_rag")
            decision.setdefault("session_id", str(payload.get("session_id") or thread_id))
            decision.setdefault("decision_owner", "MCagent LLM")
            decision.setdefault(
                "objective_contract",
                "The graph invoked the existing Agent router to obtain MCagent's tool decision. It did not use keyword routing or alter AgentMessage delivery.",
            )
        return {
            "route_decision": decision,
            **_append(
                state,
                "mcagent.route_agent_decision",
                "agent_route_decision_recorded" if decision.get("routed") else "agent_route_decision_deferred",
                {
                    "route_decision_id": decision.get("route_decision_id") or "",
                    "route_intent": decision.get("route_intent") or "",
                    "decision_owner": decision.get("decision_owner") or "MCagent LLM",
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
                agent_id="mcagent_rag",
                graph_name="MCagentGraph",
                node_name="mcagent.graph_status_route",
                runtime_request=runtime_request,
                route_decision=route_decision,
            )
        )
        adapter = graph_status_route_executor_metadata(
            agent_id="mcagent_rag",
            graph_name="MCagentGraph",
            node_name="mcagent.graph_status_route",
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
                "mcagent.graph_status_route",
                "executed_agent_selected_status",
                {
                    "agent": "mcagent_rag",
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
                agent_id="mcagent_rag",
                graph_name="MCagentGraph",
                node_name="mcagent.graph_crawler_audit_route",
                runtime_request=runtime_request,
                route_decision=route_decision,
            )
        )
        adapter = graph_crawler_audit_route_executor_metadata(
            agent_id="mcagent_rag",
            graph_name="MCagentGraph",
            node_name="mcagent.graph_crawler_audit_route",
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
                "mcagent.graph_crawler_audit_route",
                "executed_agent_selected_crawler_audit",
                {
                    "agent": "mcagent_rag",
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
        contextual_question = state.get("contextual_question_contract") if isinstance(state.get("contextual_question_contract"), dict) else {}
        route_decision_output = state.get("route_decision_output_contract") if isinstance(state.get("route_decision_output_contract"), dict) else {}
        route_execution = state.get("route_execution_contract") if isinstance(state.get("route_execution_contract"), dict) else {}
        legacy_handler_surface = state.get("legacy_handler_surface_contract") if isinstance(state.get("legacy_handler_surface_contract"), dict) else {}
        thread_id = str(state.get("thread_id") or runtime_request.get("session_id") or result.get("session_id") or "default")
        contract = build_route_result_contract(
            thread_id=thread_id,
            graph_name="MCagentGraph",
            agent_id="mcagent_rag",
            node_name="mcagent.prepare_route_result_contract",
            contract_kind="mcagent_route_result_contract",
            decision_owner="MCagent LLM",
            result=result,
            runtime_request=runtime_request,
            runtime_adapter=runtime_adapter,
            route_input_contract=route_input,
            message_preflight_contract=message_preflight,
            contextual_question_contract=contextual_question,
            route_decision_output_contract=route_decision_output,
            route_execution_contract=route_execution,
            legacy_handler_surface_contract=legacy_handler_surface,
        )
        shape = contract["result_shape"]
        return {
            "route_result_contract": contract,
            **_append(
                state,
                "mcagent.prepare_route_result_contract",
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
            graph_name="MCagentGraph",
            agent_id="mcagent_rag",
            node_name="mcagent.prepare_legacy_handler_surface_contract",
            contract_kind="mcagent_legacy_handler_surface_facts_contract",
            decision_owner="MCagent LLM",
            runtime_request=runtime_request,
            runtime_adapter=runtime_adapter,
            route_decision_output_contract=route_decision_output,
            route_execution_contract=route_execution,
        )
        return {
            "legacy_handler_surface_contract": contract,
            **_append(
                state,
                "mcagent.prepare_legacy_handler_surface_contract",
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
        contextual_question = state.get("contextual_question_contract") if isinstance(state.get("contextual_question_contract"), dict) else {}
        thread_id = str(state.get("thread_id") or runtime_request.get("session_id") or result.get("session_id") or "default")
        contract = build_route_execution_contract(
            thread_id=thread_id,
            graph_name="MCagentGraph",
            agent_id="mcagent_rag",
            node_name="mcagent.prepare_route_execution_contract",
            contract_kind="mcagent_route_execution_facts_contract",
            decision_owner="MCagent LLM",
            result=result,
            runtime_request=runtime_request,
            runtime_adapter=runtime_adapter,
            route_input_contract=route_input,
            route_decision_output_contract=route_decision_output,
            message_preflight_contract=message_preflight,
            contextual_question_contract=contextual_question,
        )
        trace_facts = contract["trace_facts"]
        result_facts = contract["result_facts"]
        return {
            "route_execution_contract": contract,
            **_append(
                state,
                "mcagent.prepare_route_execution_contract",
                "route_execution_facts_recorded",
                {
                    "contract_id": contract["contract_id"],
                    "contract_kind": contract["contract_kind"],
                    "observed_execution_stages": trace_facts["observed_execution_stages"],
                    "answer_present": result_facts["answer_present"],
                    "job_present": result_facts["job_present"],
                },
            ),
        }

    def prepare_route_decision_output_contract(state: AgentGraphState) -> dict[str, Any]:
        result = dict(state.get("result") or {})
        runtime_request = state.get("runtime_request") if isinstance(state.get("runtime_request"), dict) else {}
        runtime_adapter = state.get("runtime_adapter") if isinstance(state.get("runtime_adapter"), dict) else {}
        route_input = state.get("route_input_contract") if isinstance(state.get("route_input_contract"), dict) else {}
        message_preflight = state.get("message_preflight_contract") if isinstance(state.get("message_preflight_contract"), dict) else {}
        contextual_question = state.get("contextual_question_contract") if isinstance(state.get("contextual_question_contract"), dict) else {}
        thread_id = str(state.get("thread_id") or runtime_request.get("session_id") or result.get("session_id") or "default")
        contract = build_route_decision_output_contract(
            thread_id=thread_id,
            graph_name="MCagentGraph",
            agent_id="mcagent_rag",
            node_name="mcagent.prepare_route_decision_output_contract",
            contract_kind="mcagent_route_decision_output_facts_contract",
            decision_owner="MCagent LLM",
            result=result,
            runtime_request=runtime_request,
            runtime_adapter=runtime_adapter,
            route_input_contract=route_input,
            message_preflight_contract=message_preflight,
            contextual_question_contract=contextual_question,
        )
        facts = contract["trace_facts"]
        return {
            "route_decision_output_contract": contract,
            **_append(
                state,
                "mcagent.prepare_route_decision_output_contract",
                "route_decision_output_facts_recorded",
                {
                    "contract_id": contract["contract_id"],
                    "contract_kind": contract["contract_kind"],
                    "has_tool_selected_trace": facts["has_tool_selected_trace"],
                    "observed_selected_tool": facts["observed_selected_tool"],
                },
            ),
        }

    def finalize(state: AgentGraphState) -> dict[str, Any]:
        result = dict(state.get("result") or {})
        agent_runtime = {
            "agent_graph": "MCagentGraph",
            "agent_id": "mcagent_rag",
            "tool_boundary": state.get("tool_boundary") or {},
            "selected_tool_groups": state.get("selected_tool_groups") or {},
            "memory_context": state.get("memory_context") or {},
            "retrieval_contract": state.get("retrieval_contract") or {},
            "message_preflight_contract": state.get("message_preflight_contract") or {},
            "contextual_question_contract": state.get("contextual_question_contract") or {},
            "route_input_contract": state.get("route_input_contract") or {},
            "runtime_request": state.get("runtime_request") or {},
            "route_decision": state.get("route_decision") or {},
            "runtime_adapter": state.get("runtime_adapter") or result.get("legacy_runtime_adapter") or {},
            "route_decision_output_contract": state.get("route_decision_output_contract") or {},
            "route_execution_contract": state.get("route_execution_contract") or {},
            "legacy_handler_surface_contract": state.get("legacy_handler_surface_contract") or {},
            "route_result_contract": state.get("route_result_contract") or {},
            "visited_nodes": [*state.get("visited_nodes", []), "mcagent.finalize"],
            "events": [*state.get("graph_events", []), _event("mcagent.finalize", "ready")],
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
    builder.add_node("load_memory_boundary", load_memory_boundary)
    builder.add_node("select_local_tools", select_local_tools)
    builder.add_node("prepare_local_retrieval", prepare_local_retrieval)
    builder.add_node("prepare_message_preflight_contract", prepare_message_preflight_contract)
    builder.add_node("prepare_contextual_question_contract", prepare_contextual_question_contract)
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
    builder.add_edge("receive", "load_memory_boundary")
    builder.add_edge("load_memory_boundary", "select_local_tools")
    builder.add_edge("select_local_tools", "prepare_local_retrieval")
    builder.add_edge("prepare_local_retrieval", "prepare_message_preflight_contract")
    builder.add_edge("prepare_message_preflight_contract", "prepare_contextual_question_contract")
    builder.add_edge("prepare_contextual_question_contract", "prepare_route_input_contract")
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


def run_mcagent_graph(
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
    graph = build_mcagent_graph(
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
            "agent_id": "mcagent_rag",
            "payload": dict(payload),
            "tool_boundary": {},
            "graph_events": [],
            "visited_nodes": [],
            "errors": [],
        }
    )
    return dict(final_state.get("result") or {})
