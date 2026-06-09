from __future__ import annotations

from typing import Any, Callable

from ..config import AppConfig


EmitFn = Callable[[str, Any], None]
AgentDeliveryFn = Callable[..., dict[str, Any]]

LEGACY_RUNTIME_ADAPTER = "legacy_web_server_runtime"


def _runtime_request_summary(runtime_request: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(runtime_request, dict):
        return {}
    payload = runtime_request.get("payload") if isinstance(runtime_request.get("payload"), dict) else {}
    return {
        "runtime_request_id": runtime_request.get("request_id") or "",
        "runtime_request_node": runtime_request.get("node") or "",
        "route_input_contract_id": runtime_request.get("route_input_contract_id") or "",
        "contract_kind": runtime_request.get("contract_kind") or "",
        "session_id": runtime_request.get("session_id") or payload.get("session_id") or "",
        "payload_agent": payload.get("agent") or "",
        "payload_keys": sorted(str(key) for key in payload.keys()),
    }


def legacy_runtime_adapter_metadata(
    *,
    agent_id: str,
    graph_name: str,
    node_name: str,
    runtime_request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    display_agent = "CrawlerAgent" if agent_id == "crawler_agent" else "MCagent" if agent_id == "mcagent_rag" else agent_id
    request_summary = _runtime_request_summary(runtime_request)
    return {
        "adapter": LEGACY_RUNTIME_ADAPTER,
        "agent_id": agent_id,
        "graph": graph_name,
        "node": node_name,
        **request_summary,
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
    runtime_request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Forward graph payloads through the current web_server runtime during migration."""

    request_payload = runtime_request.get("payload") if isinstance(runtime_request, dict) and isinstance(runtime_request.get("payload"), dict) else payload
    metadata = legacy_runtime_adapter_metadata(
        agent_id=agent_id,
        graph_name=graph_name,
        node_name=node_name,
        runtime_request=runtime_request,
    )
    result = dict(agent_delivery(config, dict(request_payload), emit=emit))
    result_metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    result["metadata"] = {**result_metadata, "legacy_runtime_adapter": metadata}
    result["legacy_runtime_adapter"] = metadata
    return result
