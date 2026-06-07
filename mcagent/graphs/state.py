from __future__ import annotations

from typing import Any, NotRequired, TypedDict


class GraphEvent(TypedDict):
    node: str
    status: str
    detail: dict[str, Any]


class ConversationGraphState(TypedDict):
    thread_id: str
    incoming: dict[str, Any]
    active_agent: str
    payload: dict[str, Any]
    result: NotRequired[dict[str, Any]]
    graph_events: list[GraphEvent]
    visited_nodes: list[str]
    errors: list[dict[str, Any]]
