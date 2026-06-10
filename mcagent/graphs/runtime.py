from __future__ import annotations

from typing import Any, Callable
import threading

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from ..agent_message import AgentMessage, make_agent_message
from ..config import AppConfig
from .crawler import run_crawler_graph
from .mcagent import run_mcagent_graph
from .state import ConversationGraphState, GraphEvent


EmitFn = Callable[[str, Any], None]
AgentDeliveryFn = Callable[..., dict[str, Any]]
AgentRouteDeciderFn = Callable[..., dict[str, Any]]
StatusExecutorFn = Callable[..., dict[str, Any]]
CrawlerAuditExecutorFn = Callable[..., dict[str, Any]]
LocalCorpusInventoryExecutorFn = Callable[..., dict[str, Any]]
RouterErrorExecutorFn = Callable[..., dict[str, Any]]
DirectAnswerExecutorFn = Callable[..., dict[str, Any]]
TemporaryExtractExecutorFn = Callable[..., dict[str, Any]]
_GRAPH_CACHE_LOCK = threading.Lock()
_GRAPH_CACHE: dict[tuple[int, int, int, int, int, int, int, int, int, int], Any] = {}


def _agent_id_for_route(message: AgentMessage) -> str:
    if message.to_agent_id == "crawler_agent":
        return "crawler_agent"
    if message.to_agent_id in {"mcagent_rag", "retriever_only"}:
        return "mcagent_rag"
    return message.to_agent_id or "mcagent_rag"


def _graph_event(node: str, status: str, detail: dict[str, Any] | None = None) -> GraphEvent:
    return {"node": node, "status": status, "detail": dict(detail or {})}


def _append_event(state: ConversationGraphState, node: str, status: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "visited_nodes": [*state.get("visited_nodes", []), node],
        "graph_events": [*state.get("graph_events", []), _graph_event(node, status, detail)],
    }


def _payload_with_message(payload: dict[str, Any], message: AgentMessage) -> dict[str, Any]:
    next_payload = dict(payload)
    next_payload["agent"] = message.to_agent_id
    next_payload["question"] = message.content
    next_payload["message_from"] = message.from_agent
    next_payload["agent_message"] = message.to_dict()
    next_payload.setdefault("session_id", message.conversation_id or payload.get("session_id") or "default")
    return next_payload


def _agent_runtime_node(prefix: str, adapter_name: str) -> str:
    if adapter_name == "graph_status_route_executor":
        return f"{prefix}.graph_status_route"
    if adapter_name == "graph_crawler_audit_route_executor":
        return f"{prefix}.graph_crawler_audit_route"
    if adapter_name == "graph_local_corpus_inventory_route_executor":
        return f"{prefix}.graph_local_corpus_inventory_route"
    if adapter_name == "graph_router_error_route_executor":
        return f"{prefix}.graph_router_error_route"
    if adapter_name == "graph_direct_answer_node_executor":
        return f"{prefix}.graph_direct_answer_node"
    if adapter_name == "graph_temporary_extract_node_executor":
        return f"{prefix}.graph_temporary_extract_node"
    return f"{prefix}.legacy_adapter"


def build_conversation_graph(
    config: AppConfig,
    agent_delivery: AgentDeliveryFn,
    emit: EmitFn | None = None,
    *,
    route_decider: AgentRouteDeciderFn | None = None,
    status_executor: StatusExecutorFn | None = None,
    crawler_audit_executor: CrawlerAuditExecutorFn | None = None,
    local_corpus_inventory_executor: LocalCorpusInventoryExecutorFn | None = None,
    router_error_executor: RouterErrorExecutorFn | None = None,
    direct_answer_executor: DirectAnswerExecutorFn | None = None,
    temporary_extract_executor: TemporaryExtractExecutorFn | None = None,
):
    builder = StateGraph(ConversationGraphState)

    def receive(state: ConversationGraphState) -> dict[str, Any]:
        incoming = state["incoming"]
        message = make_agent_message(
            incoming.get("from_agent") or "User",
            incoming.get("content") or "",
            incoming.get("to_agent") or "MCagent",
            intent=str(incoming.get("intent") or ""),
            conversation_id=str(incoming.get("conversation_id") or state.get("thread_id") or ""),
            reply_to=str(incoming.get("reply_to") or ""),
            requires_reply=bool(incoming.get("requires_reply", True)),
            metadata=incoming.get("metadata") if isinstance(incoming.get("metadata"), dict) else {},
        )
        payload = _payload_with_message(state.get("payload", {}), message)
        return {
            "incoming": message.to_dict(),
            "payload": payload,
            "active_agent": _agent_id_for_route(message),
            **_append_event(
                state,
                "conversation.receive",
                "message_normalized",
                {
                    "from_agent": message.from_agent,
                    "to_agent": message.to_agent,
                    "intent": message.intent,
                },
            ),
        }

    def route(state: ConversationGraphState) -> dict[str, Any]:
        active_agent = str(state.get("active_agent") or "")
        return _append_event(state, "conversation.route", "target_selected", {"active_agent": active_agent})

    def run_mcagent(state: ConversationGraphState) -> dict[str, Any]:
        payload = dict(state.get("payload") or {})
        payload["agent"] = "mcagent_rag"
        result = run_mcagent_graph(
            config,
            payload,
            agent_delivery=agent_delivery,
            emit=emit,
            thread_id=str(state.get("thread_id") or "default"),
            route_decider=route_decider,
            status_executor=status_executor,
            crawler_audit_executor=crawler_audit_executor,
            local_corpus_inventory_executor=local_corpus_inventory_executor,
            router_error_executor=router_error_executor,
            direct_answer_executor=direct_answer_executor,
            temporary_extract_executor=temporary_extract_executor,
        )
        agent_runtime = result.get("agent_graph_runtime") if isinstance(result.get("agent_graph_runtime"), dict) else {}
        adapter = agent_runtime.get("runtime_adapter") if isinstance(agent_runtime.get("runtime_adapter"), dict) else {}
        adapter_name = str(adapter.get("adapter") or "agent_graph_runtime")
        runtime_node = _agent_runtime_node("mcagent_graph", adapter_name)
        return {
            "result": result,
            **_append_event(
                state,
                runtime_node,
                "agent_graph_runtime_completed",
                {"agent": "mcagent_rag", "adapter": adapter_name},
            ),
        }

    def run_crawler(state: ConversationGraphState) -> dict[str, Any]:
        payload = dict(state.get("payload") or {})
        payload["agent"] = "crawler_agent"
        result = run_crawler_graph(
            config,
            payload,
            agent_delivery=agent_delivery,
            emit=emit,
            thread_id=str(state.get("thread_id") or "default"),
            route_decider=route_decider,
            status_executor=status_executor,
            crawler_audit_executor=crawler_audit_executor,
            local_corpus_inventory_executor=local_corpus_inventory_executor,
            router_error_executor=router_error_executor,
            direct_answer_executor=direct_answer_executor,
            temporary_extract_executor=temporary_extract_executor,
        )
        agent_runtime = result.get("agent_graph_runtime") if isinstance(result.get("agent_graph_runtime"), dict) else {}
        adapter = agent_runtime.get("runtime_adapter") if isinstance(agent_runtime.get("runtime_adapter"), dict) else {}
        adapter_name = str(adapter.get("adapter") or "agent_graph_runtime")
        runtime_node = _agent_runtime_node("crawler_graph", adapter_name)
        return {
            "result": result,
            **_append_event(
                state,
                runtime_node,
                "agent_graph_runtime_completed",
                {"agent": "crawler_agent", "adapter": adapter_name},
            ),
        }

    def run_unknown(state: ConversationGraphState) -> dict[str, Any]:
        payload = dict(state.get("payload") or {})
        result = agent_delivery(config, payload, emit=emit)
        return {
            "result": result,
            **_append_event(state, "conversation.unknown_target", "agent_runtime_completed", {"agent": state.get("active_agent") or ""}),
        }

    def emit_response(state: ConversationGraphState) -> dict[str, Any]:
        event_update = _append_event(state, "conversation.emit_response", "ready", {"active_agent": state.get("active_agent") or ""})
        visited_nodes = event_update["visited_nodes"]
        graph_events = event_update["graph_events"]
        result = dict(state.get("result") or {})
        graph_runtime = {
            "runtime": "langgraph",
            "thread_id": state.get("thread_id") or "",
            "active_agent": state.get("active_agent") or "",
            "visited_nodes": visited_nodes,
            "events": graph_events,
        }
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        metadata = {**metadata, "graph_runtime": graph_runtime}
        result["metadata"] = metadata
        result["graph_runtime"] = graph_runtime
        return {
            "result": result,
            **event_update,
        }

    def route_key(state: ConversationGraphState) -> str:
        active_agent = str(state.get("active_agent") or "")
        if active_agent == "crawler_agent":
            return "crawler"
        if active_agent in {"mcagent_rag", "retriever_only"}:
            return "mcagent"
        return "unknown"

    builder.add_node("receive", receive)
    builder.add_node("route", route)
    builder.add_node("mcagent", run_mcagent)
    builder.add_node("crawler", run_crawler)
    builder.add_node("unknown", run_unknown)
    builder.add_node("emit_response", emit_response)
    builder.add_edge(START, "receive")
    builder.add_edge("receive", "route")
    builder.add_conditional_edges("route", route_key, {"mcagent": "mcagent", "crawler": "crawler", "unknown": "unknown"})
    builder.add_edge("mcagent", "emit_response")
    builder.add_edge("crawler", "emit_response")
    builder.add_edge("unknown", "emit_response")
    builder.add_edge("emit_response", END)
    return builder.compile(checkpointer=MemorySaver())


def dispatch_agent_message_graph(
    config: AppConfig,
    payload: dict[str, Any],
    *,
    from_agent: str,
    content: str,
    to_agent: str,
    agent_delivery: AgentDeliveryFn,
    emit: EmitFn | None = None,
    intent: str = "",
    conversation_id: str = "",
    reply_to: str = "",
    requires_reply: bool = True,
    metadata: dict[str, Any] | None = None,
    route_decider: AgentRouteDeciderFn | None = None,
    status_executor: StatusExecutorFn | None = None,
    crawler_audit_executor: CrawlerAuditExecutorFn | None = None,
    local_corpus_inventory_executor: LocalCorpusInventoryExecutorFn | None = None,
    router_error_executor: RouterErrorExecutorFn | None = None,
    direct_answer_executor: DirectAnswerExecutorFn | None = None,
    temporary_extract_executor: TemporaryExtractExecutorFn | None = None,
) -> dict[str, Any]:
    """Deliver a From-Content-To message through the LangGraph conversation runtime.

    The graph router only looks at AgentMessage.to_agent. The receiving Agent still
    owns all semantic decisions and tool choices inside its own node/subgraph.
    """

    thread_id = str(conversation_id or payload.get("session_id") or payload.get("conversation_id") or "default")
    if emit is None:
        cache_key = (
            id(config),
            id(agent_delivery),
            id(route_decider),
            id(status_executor),
            id(crawler_audit_executor),
            id(local_corpus_inventory_executor),
            id(router_error_executor),
            id(direct_answer_executor),
            id(temporary_extract_executor),
            0,
        )
        with _GRAPH_CACHE_LOCK:
            graph = _GRAPH_CACHE.get(cache_key)
            if graph is None:
                graph = build_conversation_graph(
                    config,
                    agent_delivery,
                    emit=None,
                    route_decider=route_decider,
                    status_executor=status_executor,
                    crawler_audit_executor=crawler_audit_executor,
                    local_corpus_inventory_executor=local_corpus_inventory_executor,
                    router_error_executor=router_error_executor,
                    direct_answer_executor=direct_answer_executor,
                    temporary_extract_executor=temporary_extract_executor,
                )
                _GRAPH_CACHE[cache_key] = graph
    else:
        graph = build_conversation_graph(
            config,
            agent_delivery,
            emit=emit,
            route_decider=route_decider,
            status_executor=status_executor,
            crawler_audit_executor=crawler_audit_executor,
            local_corpus_inventory_executor=local_corpus_inventory_executor,
            router_error_executor=router_error_executor,
            direct_answer_executor=direct_answer_executor,
            temporary_extract_executor=temporary_extract_executor,
        )
    initial_state: ConversationGraphState = {
        "thread_id": thread_id,
        "incoming": {
            "from_agent": from_agent,
            "content": content,
            "to_agent": to_agent,
            "intent": intent,
            "conversation_id": thread_id,
            "reply_to": reply_to,
            "requires_reply": requires_reply,
            "metadata": metadata if isinstance(metadata, dict) else {},
        },
        "active_agent": "",
        "payload": dict(payload),
        "graph_events": [],
        "visited_nodes": [],
        "errors": [],
    }
    if emit is not None:
        emit("graph", {"runtime": "langgraph", "thread_id": thread_id, "status": "start"})
    final_state = graph.invoke(initial_state, config={"configurable": {"thread_id": thread_id}})
    result = dict(final_state.get("result") or {})
    graph_runtime = dict(result.get("graph_runtime") or {})
    if graph_runtime:
        graph_runtime["visited_nodes"] = final_state.get("visited_nodes", graph_runtime.get("visited_nodes", []))
        graph_runtime["events"] = final_state.get("graph_events", graph_runtime.get("events", []))
        result["graph_runtime"] = graph_runtime
        metadata_out = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        metadata_out = {**metadata_out, "graph_runtime": graph_runtime}
        result["metadata"] = metadata_out
    if emit is not None:
        emit("graph", {"runtime": "langgraph", "thread_id": thread_id, "status": "done", "graph_runtime": graph_runtime})
    return result
