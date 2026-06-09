from __future__ import annotations

from typing import Any, NotRequired, TypedDict

from .state import GraphEvent


class AgentGraphState(TypedDict):
    thread_id: str
    agent_id: str
    payload: dict[str, Any]
    result: NotRequired[dict[str, Any]]
    tool_boundary: dict[str, Any]
    selected_tool_groups: NotRequired[dict[str, Any]]
    memory_context: NotRequired[dict[str, Any]]
    retrieval_contract: NotRequired[dict[str, Any]]
    mission_contract: NotRequired[dict[str, Any]]
    route_input_contract: NotRequired[dict[str, Any]]
    runtime_request: NotRequired[dict[str, Any]]
    runtime_adapter: NotRequired[dict[str, Any]]
    graph_events: list[GraphEvent]
    visited_nodes: list[str]
    errors: list[dict[str, Any]]
