from __future__ import annotations

from typing import Any


GRAPH_STATUS_ROUTE_EXECUTOR = "graph_status_route_executor"
GRAPH_CRAWLER_AUDIT_ROUTE_EXECUTOR = "graph_crawler_audit_route_executor"
GRAPH_LOCAL_CORPUS_INVENTORY_ROUTE_EXECUTOR = "graph_local_corpus_inventory_route_executor"
GRAPH_ROUTER_ERROR_ROUTE_EXECUTOR = "graph_router_error_route_executor"
GRAPH_DIRECT_ANSWER_NODE_EXECUTOR = "graph_direct_answer_node_executor"
GRAPH_TEMPORARY_EXTRACT_NODE_EXECUTOR = "graph_temporary_extract_node_executor"
GRAPH_AGENT_MESSAGE_ROUTE_EXECUTOR = "graph_agent_message_route_executor"
GRAPH_MCAGENT_CONTEXT_REPLY_EXECUTOR = "graph_mcagent_context_reply_executor"
GRAPH_CRAWLER_MCAGENT_CONTEXT_ROUTE_EXECUTOR = "graph_crawler_mcagent_context_route_executor"
GRAPH_MCAGENT_INVENTORY_PLANNED_WORKFLOW_EXECUTOR = "graph_mcagent_inventory_planned_workflow_executor"
GRAPH_CRAWLER_PLANNED_WORKFLOW_EXECUTOR = "graph_crawler_planned_workflow_executor"
GRAPH_CRAWLER_DELEGATE_ROUTE_EXECUTOR = "graph_crawler_delegate_route_executor"
GRAPH_RAG_ANSWER_ROUTE_EXECUTOR = "graph_rag_answer_route_executor"


def _display_agent(agent_id: str) -> str:
    return "CrawlerAgent" if agent_id == "crawler_agent" else "MCagent" if agent_id == "mcagent_rag" else agent_id


def graph_route_decision_allows_execution(route_decision: dict[str, Any] | None) -> bool:
    route_decision = route_decision if isinstance(route_decision, dict) else {}
    confirmation = route_decision.get("route_confirmation") if isinstance(route_decision.get("route_confirmation"), dict) else {}
    return bool(confirmation.get("proceed", True))


def graph_route_decision_has_action_tool(route_decision: dict[str, Any] | None, tool_name: str) -> bool:
    route_decision = route_decision if isinstance(route_decision, dict) else {}
    wanted = tool_name.strip().lower()
    action_plan = route_decision.get("action_plan") if isinstance(route_decision.get("action_plan"), list) else []
    tool_decision = route_decision.get("tool_decision") if isinstance(route_decision.get("tool_decision"), dict) else {}
    tool_decision_plan = tool_decision.get("action_plan") if isinstance(tool_decision.get("action_plan"), list) else []
    for item in [*action_plan, *tool_decision_plan]:
        if isinstance(item, dict) and str(item.get("tool") or "").strip().lower() == wanted:
            return True
    return bool(route_decision.get("planned_delegate")) and wanted == "delegate_crawler"


def _base_route_executor_metadata(
    *,
    adapter: str,
    migration_status: str,
    route_label: str,
    agent_id: str,
    graph_name: str,
    node_name: str,
    runtime_request: dict[str, Any] | None = None,
    route_decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    runtime_request = runtime_request if isinstance(runtime_request, dict) else {}
    route_decision = route_decision if isinstance(route_decision, dict) else {}
    display_agent = _display_agent(agent_id)
    return {
        "adapter": adapter,
        "agent_id": agent_id,
        "graph": graph_name,
        "node": node_name,
        "runtime_request_id": runtime_request.get("request_id") or "",
        "contract_kind": runtime_request.get("contract_kind") or "",
        "session_id": runtime_request.get("session_id") or "",
        "message_preflight_contract_id": runtime_request.get("message_preflight_contract_id") or "",
        "contextual_question_contract_id": runtime_request.get("contextual_question_contract_id") or "",
        "source_planning_contract_id": runtime_request.get("source_planning_contract_id") or "",
        "side_effect_authorization_contract_id": runtime_request.get("side_effect_authorization_contract_id") or "",
        "route_input_contract_id": runtime_request.get("route_input_contract_id") or "",
        "route_decision_id": route_decision.get("route_decision_id") or "",
        "route_intent": route_decision.get("route_intent") or "",
        "migration_status": migration_status,
        "decision_owner": f"{display_agent} LLM",
        "route_decision_executed_by_graph": bool(route_decision.get("routed")),
        "legacy_runtime_adapter_bypassed": True,
        "side_effect_executed": False,
        "objective_boundary": (
            f"The graph executed only the already Agent-selected, side-effect-free {route_label} route. "
            "It did not choose a tool, start jobs, judge evidence, persist data, or alter AgentMessage routing."
        ),
    }


def _base_answer_node_metadata(
    *,
    adapter: str,
    migration_status: str,
    agent_id: str,
    graph_name: str,
    node_name: str,
    runtime_request: dict[str, Any] | None = None,
    route_decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    runtime_request = runtime_request if isinstance(runtime_request, dict) else {}
    route_decision = route_decision if isinstance(route_decision, dict) else {}
    display_agent = _display_agent(agent_id)
    return {
        "adapter": adapter,
        "agent_id": agent_id,
        "graph": graph_name,
        "node": node_name,
        "runtime_request_id": runtime_request.get("request_id") or "",
        "contract_kind": runtime_request.get("contract_kind") or "",
        "session_id": runtime_request.get("session_id") or "",
        "message_preflight_contract_id": runtime_request.get("message_preflight_contract_id") or "",
        "contextual_question_contract_id": runtime_request.get("contextual_question_contract_id") or "",
        "source_planning_contract_id": runtime_request.get("source_planning_contract_id") or "",
        "side_effect_authorization_contract_id": runtime_request.get("side_effect_authorization_contract_id") or "",
        "route_input_contract_id": runtime_request.get("route_input_contract_id") or "",
        "route_decision_id": route_decision.get("route_decision_id") or "",
        "route_intent": route_decision.get("route_intent") or "",
        "migration_status": migration_status,
        "decision_owner": f"{display_agent} LLM",
        "route_decision_executed_by_graph": bool(route_decision.get("routed")),
        "legacy_runtime_adapter_bypassed": True,
        "side_effect_executed": False,
        "agent_answer_generation": True,
        "objective_boundary": (
            "The graph executed only the already Agent-selected direct-answer final response node. "
            "This node may call the receiving Agent's answer model, but it does not choose a tool, "
            "start jobs, judge evidence, persist data, or alter AgentMessage routing."
        ),
    }


def graph_status_route_executor_metadata(
    *,
    agent_id: str,
    graph_name: str,
    node_name: str,
    runtime_request: dict[str, Any] | None = None,
    route_decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _base_route_executor_metadata(
        adapter=GRAPH_STATUS_ROUTE_EXECUTOR,
        migration_status="graph_status_route_migrated",
        route_label="status",
        agent_id=agent_id,
        graph_name=graph_name,
        node_name=node_name,
        runtime_request=runtime_request,
        route_decision=route_decision,
    )


def graph_crawler_audit_route_executor_metadata(
    *,
    agent_id: str,
    graph_name: str,
    node_name: str,
    runtime_request: dict[str, Any] | None = None,
    route_decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _base_route_executor_metadata(
        adapter=GRAPH_CRAWLER_AUDIT_ROUTE_EXECUTOR,
        migration_status="graph_crawler_audit_route_migrated",
        route_label="crawler_audit",
        agent_id=agent_id,
        graph_name=graph_name,
        node_name=node_name,
        runtime_request=runtime_request,
        route_decision=route_decision,
    )


def graph_local_corpus_inventory_route_executor_metadata(
    *,
    agent_id: str,
    graph_name: str,
    node_name: str,
    runtime_request: dict[str, Any] | None = None,
    route_decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _base_route_executor_metadata(
        adapter=GRAPH_LOCAL_CORPUS_INVENTORY_ROUTE_EXECUTOR,
        migration_status="graph_local_corpus_inventory_route_migrated",
        route_label="local_corpus_inventory",
        agent_id=agent_id,
        graph_name=graph_name,
        node_name=node_name,
        runtime_request=runtime_request,
        route_decision=route_decision,
    )


def graph_mcagent_inventory_planned_workflow_executor_metadata(
    *,
    agent_id: str,
    graph_name: str,
    node_name: str,
    runtime_request: dict[str, Any] | None = None,
    route_decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = _base_route_executor_metadata(
        adapter=GRAPH_MCAGENT_INVENTORY_PLANNED_WORKFLOW_EXECUTOR,
        migration_status="graph_mcagent_inventory_planned_workflow_migrated",
        route_label="mcagent_inventory_planned_workflow",
        agent_id=agent_id,
        graph_name=graph_name,
        node_name=node_name,
        runtime_request=runtime_request,
        route_decision=route_decision,
    )
    metadata["side_effect_executed"] = True
    metadata["objective_boundary"] = (
        "The graph executed the already MCagent-selected planned workflow whose first step is local_corpus_inventory "
        "and whose later step is one From-Content-To AgentMessage to CrawlerAgent. The graph does not choose CrawlerAgent's tools; "
        "CrawlerAgent still owns any collection, save, or ingest decision after receiving the message."
    )
    return metadata


def graph_crawler_planned_workflow_executor_metadata(
    *,
    agent_id: str,
    graph_name: str,
    node_name: str,
    runtime_request: dict[str, Any] | None = None,
    route_decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = _base_route_executor_metadata(
        adapter=GRAPH_CRAWLER_PLANNED_WORKFLOW_EXECUTOR,
        migration_status="graph_crawler_planned_workflow_migrated",
        route_label="crawler_planned_workflow",
        agent_id=agent_id,
        graph_name=graph_name,
        node_name=node_name,
        runtime_request=runtime_request,
        route_decision=route_decision,
    )
    metadata["side_effect_executed"] = True
    metadata["objective_boundary"] = (
        "The graph executed the already CrawlerAgent-selected planned workflow with delegate_crawler in its action_plan. "
        "The graph does not choose sources, search terms, or acceptance decisions; CrawlerAgent's selected workflow and job runtime own those decisions."
    )
    return metadata


def graph_crawler_delegate_route_executor_metadata(
    *,
    agent_id: str,
    graph_name: str,
    node_name: str,
    runtime_request: dict[str, Any] | None = None,
    route_decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = _base_route_executor_metadata(
        adapter=GRAPH_CRAWLER_DELEGATE_ROUTE_EXECUTOR,
        migration_status="graph_crawler_delegate_route_migrated",
        route_label="crawler_delegate_crawler",
        agent_id=agent_id,
        graph_name=graph_name,
        node_name=node_name,
        runtime_request=runtime_request,
        route_decision=route_decision,
    )
    metadata["side_effect_executed"] = True
    metadata["objective_boundary"] = (
        "The graph executed the already CrawlerAgent-selected delegate_crawler route. "
        "The graph does not choose sources, search terms, acceptance decisions, or persistence policy; "
        "CrawlerAgent and its job runtime own those decisions after receiving the AgentMessage."
    )
    return metadata


def graph_rag_answer_route_executor_metadata(
    *,
    agent_id: str,
    graph_name: str,
    node_name: str,
    runtime_request: dict[str, Any] | None = None,
    route_decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = _base_route_executor_metadata(
        adapter=GRAPH_RAG_ANSWER_ROUTE_EXECUTOR,
        migration_status="graph_rag_answer_route_migrated",
        route_label="rag_answer_generation",
        agent_id=agent_id,
        graph_name=graph_name,
        node_name=node_name,
        runtime_request=runtime_request,
        route_decision=route_decision,
    )
    metadata["objective_boundary"] = (
        "The graph executed the already Agent-selected local RAG/evidence answer route. "
        "The graph does not choose public-web tools or CrawlerAgent tools; evidence selection, sufficiency review, "
        "and any AgentMessage handoff remain explicit Agent-owned decisions recorded in the route trace."
    )
    return metadata


def graph_router_error_route_executor_metadata(
    *,
    agent_id: str,
    graph_name: str,
    node_name: str,
    runtime_request: dict[str, Any] | None = None,
    route_decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _base_route_executor_metadata(
        adapter=GRAPH_ROUTER_ERROR_ROUTE_EXECUTOR,
        migration_status="graph_router_error_route_migrated",
        route_label="router_error",
        agent_id=agent_id,
        graph_name=graph_name,
        node_name=node_name,
        runtime_request=runtime_request,
        route_decision=route_decision,
    )


def graph_direct_answer_node_executor_metadata(
    *,
    agent_id: str,
    graph_name: str,
    node_name: str,
    runtime_request: dict[str, Any] | None = None,
    route_decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _base_answer_node_metadata(
        adapter=GRAPH_DIRECT_ANSWER_NODE_EXECUTOR,
        migration_status="graph_direct_answer_node_migrated",
        agent_id=agent_id,
        graph_name=graph_name,
        node_name=node_name,
        runtime_request=runtime_request,
        route_decision=route_decision,
    )


def graph_temporary_extract_node_executor_metadata(
    *,
    agent_id: str,
    graph_name: str,
    node_name: str,
    runtime_request: dict[str, Any] | None = None,
    route_decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = _base_answer_node_metadata(
        adapter=GRAPH_TEMPORARY_EXTRACT_NODE_EXECUTOR,
        migration_status="graph_temporary_extract_node_migrated",
        agent_id=agent_id,
        graph_name=graph_name,
        node_name=node_name,
        runtime_request=runtime_request,
        route_decision=route_decision,
    )
    metadata.update(
        {
            "agent_answer_generation": True,
            "temporary_extract": True,
            "saved_to_local": False,
            "objective_boundary": (
                "The graph executed only the already Agent-selected temporary_extract node. "
                "This node may temporarily read a public URL and call the receiving Agent's summary/review model, "
                "but it does not choose sources, start background jobs, persist evidence, or upgrade to delegate_crawler."
            ),
        }
    )
    return metadata


def graph_agent_message_route_executor_metadata(
    *,
    agent_id: str,
    graph_name: str,
    node_name: str,
    runtime_request: dict[str, Any] | None = None,
    route_decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = _base_route_executor_metadata(
        adapter=GRAPH_AGENT_MESSAGE_ROUTE_EXECUTOR,
        migration_status="graph_agent_message_route_migrated",
        route_label="agent_message",
        agent_id=agent_id,
        graph_name=graph_name,
        node_name=node_name,
        runtime_request=runtime_request,
        route_decision=route_decision,
    )
    metadata.update(
        {
            "agent_message": True,
            "side_effect_executed": False,
            "objective_boundary": (
                "The graph executed only the already Agent-selected no-persistence AgentMessage route. "
                "It delivers one From-Content-To message and returns the receiver's reply. It does not infer "
                "the target from keywords, start jobs, persist evidence, or choose the receiver's next tool."
            ),
        }
    )
    return metadata


def graph_mcagent_context_reply_executor_metadata(
    *,
    agent_id: str,
    graph_name: str,
    node_name: str,
    runtime_request: dict[str, Any] | None = None,
    route_decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = _base_route_executor_metadata(
        adapter=GRAPH_MCAGENT_CONTEXT_REPLY_EXECUTOR,
        migration_status="graph_mcagent_context_reply_migrated",
        route_label="mcagent_context_reply",
        agent_id=agent_id,
        graph_name=graph_name,
        node_name=node_name,
        runtime_request=runtime_request,
        route_decision=route_decision,
    )
    metadata.update(
        {
            "agent_message": True,
            "mcagent_context_reply": True,
            "side_effect_executed": False,
            "objective_boundary": (
                "The graph executed only MCagent's no-persistence reply to a CrawlerAgent "
                "AgentMessage asking for local context. It reads local evidence, returns objective "
                "context/gaps over the same From-Content-To bus, and does not start Crawler jobs, "
                "choose CrawlerAgent tools, persist evidence, or alter message routing."
            ),
        }
    )
    return metadata


def graph_crawler_mcagent_context_route_executor_metadata(
    *,
    agent_id: str,
    graph_name: str,
    node_name: str,
    runtime_request: dict[str, Any] | None = None,
    route_decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = _base_route_executor_metadata(
        adapter=GRAPH_CRAWLER_MCAGENT_CONTEXT_ROUTE_EXECUTOR,
        migration_status="graph_crawler_mcagent_context_route_migrated",
        route_label="crawler_mcagent_context",
        agent_id=agent_id,
        graph_name=graph_name,
        node_name=node_name,
        runtime_request=runtime_request,
        route_decision=route_decision,
    )
    metadata.update(
        {
            "agent_message": True,
            "mcagent_context": True,
            "side_effect_executed": False,
            "objective_boundary": (
                "The graph executed only CrawlerAgent's already-selected no-persistence mcagent_context route. "
                "It sends one From-Content-To AgentMessage to MCagent asking for local context/gaps and returns "
                "MCagent's reply. It does not start collection jobs, choose public sources, persist evidence, "
                "or decide whether the returned context is sufficient."
            ),
        }
    )
    return metadata
