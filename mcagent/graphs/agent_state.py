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
    graph_events: list[GraphEvent]
    visited_nodes: list[str]
    errors: list[dict[str, Any]]
