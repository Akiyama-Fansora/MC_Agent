from __future__ import annotations

from typing import Any, Callable

from ..config import AppConfig


EmitFn = Callable[[str, Any], None]
AgentDeliveryFn = Callable[..., dict[str, Any]]

LEGACY_RUNTIME_ADAPTER = "legacy_web_server_runtime"


def legacy_runtime_adapter_metadata(*, agent_id: str, graph_name: str, node_name: str) -> dict[str, Any]:
    display_agent = "CrawlerAgent" if agent_id == "crawler_agent" else "MCagent" if agent_id == "mcagent_rag" else agent_id
    return {
        "adapter": LEGACY_RUNTIME_ADAPTER,
        "agent_id": agent_id,
        "graph": graph_name,
        "node": node_name,
        "migration_status": "legacy_runtime_adapter",
        "decision_owner": f"{display_agent} LLM",
        "objective_boundary": (
            "The adapter only forwards the graph payload to the injected legacy delivery function and records "
            "migration metadata. It does not choose tools, judge evidence, or override AgentMessage routing."
        ),
    }


def deliver_via_legacy_runtime(
    config: AppConfig,
    payload: dict[str, Any],
    *,
    agent_delivery: AgentDeliveryFn,
    emit: EmitFn | None = None,
    agent_id: str,
    graph_name: str,
    node_name: str,
) -> dict[str, Any]:
    """Forward graph payloads through the current web_server runtime during migration."""

    metadata = legacy_runtime_adapter_metadata(agent_id=agent_id, graph_name=graph_name, node_name=node_name)
    result = dict(agent_delivery(config, dict(payload), emit=emit))
    result_metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    result["metadata"] = {**result_metadata, "legacy_runtime_adapter": metadata}
    result["legacy_runtime_adapter"] = metadata
    return result
