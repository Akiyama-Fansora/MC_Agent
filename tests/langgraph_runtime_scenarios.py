from __future__ import annotations

from pathlib import Path
import sys
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.config import (  # noqa: E402
    AppConfig,
    ChunkingConfig,
    EmbeddingConfig,
    OllamaConfig,
    PathsConfig,
    RetrievalConfig,
)
from mcagent.graphs import dispatch_agent_message_graph  # noqa: E402
from mcagent.graphs import runtime as graph_runtime_module  # noqa: E402


def make_temp_config(root: Path) -> AppConfig:
    data = root / "data"
    source = data / "crawler_exports"
    source.mkdir(parents=True, exist_ok=True)
    return AppConfig(
        paths=PathsConfig(
            project_root=root,
            source_dir=source,
            db_path=data / "mcagent.sqlite",
            index_path=data / "vector_index.npz",
        ),
        embedding=EmbeddingConfig(),
        chunking=ChunkingConfig(),
        retrieval=RetrievalConfig(),
        ollama=OllamaConfig(timeout_seconds=1),
    )


def assert_true(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name}: {detail}")


def test_conversation_graph_routes_only_by_message_target() -> None:
    calls: list[dict[str, Any]] = []

    def legacy(config: AppConfig, payload: dict[str, Any], emit: Any | None = None) -> dict[str, Any]:  # noqa: ARG001
        calls.append(dict(payload))
        return {
            "answer": f"delivered to {payload.get('agent')}",
            "agent": payload.get("agent"),
            "sources": [],
            "context": "",
        }

    with tempfile.TemporaryDirectory() as tmp:
        result = dispatch_agent_message_graph(
            make_temp_config(Path(tmp)),
            {"session_id": "graph-route", "question": "Crawler please ignore this text and answer locally"},
            from_agent="User",
            content="This content names CrawlerAgent but is addressed to MCagent.",
            to_agent="MCagent",
            conversation_id="graph-route",
            legacy_delivery=legacy,
        )
    assert_true("legacy_called_once", len(calls) == 1, str(calls))
    assert_true("routed_to_mcagent", calls[0].get("agent") == "mcagent_rag", str(calls[0]))
    runtime = result.get("graph_runtime") or {}
    assert_true("runtime_is_langgraph", runtime.get("runtime") == "langgraph", str(runtime))
    assert_true("active_agent_mcagent", runtime.get("active_agent") == "mcagent_rag", str(runtime))
    assert_true("mcagent_node_visited", "mcagent_graph.legacy_delivery" in runtime.get("visited_nodes", []), str(runtime))
    assert_true("crawler_node_not_visited", "crawler_graph.legacy_delivery" not in runtime.get("visited_nodes", []), str(runtime))
    agent_runtime = result.get("agent_graph_runtime") or {}
    assert_true("mcagent_subgraph", agent_runtime.get("agent_graph") == "MCagentGraph", str(agent_runtime))
    assert_true("mcagent_select_local_tools_node", "mcagent.select_local_tools" in agent_runtime.get("visited_nodes", []), str(agent_runtime))
    boundary = agent_runtime.get("tool_boundary") or {}
    assert_true("mcagent_local_only", "local_rag" in boundary.get("allowed_capability_groups", []), str(boundary))
    assert_true("mcagent_blocks_web", "web_search" in boundary.get("blocked_capability_groups", []), str(boundary))
    selected_groups = agent_runtime.get("selected_tool_groups") or {}
    local_tools = set(selected_groups.get("default_tools") or [])
    assert_true("mcagent_default_local_group", selected_groups.get("default_groups") == ["local"], str(selected_groups))
    assert_true("mcagent_local_tools_include_rag", "local_rag_search" in local_tools, str(selected_groups))
    assert_true("mcagent_local_tools_include_message_handoff", "delegate_crawler" in local_tools, str(selected_groups))
    assert_true("mcagent_local_tools_exclude_crawler_web", not {"web_discovery", "fetch_url", "playwright", "browser_collect", "modpack_download"} & local_tools, str(selected_groups))


def test_conversation_graph_can_dispatch_to_crawler_node() -> None:
    calls: list[dict[str, Any]] = []

    def legacy(config: AppConfig, payload: dict[str, Any], emit: Any | None = None) -> dict[str, Any]:  # noqa: ARG001
        calls.append(dict(payload))
        return {
            "answer": "crawler received",
            "agent": payload.get("agent"),
            "sources": [],
            "context": "",
        }

    with tempfile.TemporaryDirectory() as tmp:
        result = dispatch_agent_message_graph(
            make_temp_config(Path(tmp)),
            {"session_id": "graph-crawler"},
            from_agent="MCagent",
            content="Collect public data for this target.",
            to_agent="CrawlerAgent",
            intent="collection_request",
            conversation_id="graph-crawler",
            metadata={"delivery_target": "MCagent/RAG"},
            legacy_delivery=legacy,
        )
    assert_true("legacy_called_once", len(calls) == 1, str(calls))
    assert_true("routed_to_crawler", calls[0].get("agent") == "crawler_agent", str(calls[0]))
    message = calls[0].get("agent_message") or {}
    assert_true("message_preserved", message.get("from_agent") == "MCagent" and message.get("to_agent") == "CrawlerAgent", str(message))
    runtime = result.get("graph_runtime") or {}
    assert_true("crawler_node_visited", "crawler_graph.legacy_delivery" in runtime.get("visited_nodes", []), str(runtime))
    agent_runtime = result.get("agent_graph_runtime") or {}
    assert_true("crawler_subgraph", agent_runtime.get("agent_graph") == "CrawlerAgentGraph", str(agent_runtime))
    assert_true("crawler_select_tool_groups_node", "crawler.select_tool_groups" in agent_runtime.get("visited_nodes", []), str(agent_runtime))
    boundary = agent_runtime.get("tool_boundary") or {}
    assert_true("crawler_general_web", "web_discovery" in boundary.get("allowed_capability_groups", []), str(boundary))
    assert_true("crawler_optional_domain_toolsets", "optional_domain_toolsets" in boundary.get("allowed_capability_groups", []), str(boundary))
    general_tools = set(boundary.get("general_collection_tools") or [])
    minecraft_tools = set((boundary.get("domain_toolsets") or {}).get("minecraft") or [])
    assert_true("crawler_general_tools_include_fetch", {"web_discovery", "fetch_url", "playwright"}.issubset(general_tools), str(boundary))
    assert_true("crawler_general_tools_exclude_minecraft", not {"mcmod", "modrinth", "modpack_download", "modpack_internal"} & general_tools, str(boundary))
    assert_true("crawler_minecraft_domain_tools", {"mcmod", "modrinth", "modpack_download", "modpack_internal"}.issubset(minecraft_tools), str(boundary))
    selected_groups = agent_runtime.get("selected_tool_groups") or {}
    assert_true("crawler_default_general_only", selected_groups.get("default_groups") == ["general"], str(selected_groups))
    assert_true("crawler_domain_candidates_visible", "minecraft" in (selected_groups.get("candidate_domain_toolsets") or {}), str(selected_groups))
    assert_true("crawler_selection_owned_by_llm", selected_groups.get("decision_owner") == "CrawlerAgent LLM", str(selected_groups))


def test_non_streaming_graph_reuses_checkpointed_runtime_without_reusing_emit() -> None:
    calls: list[str] = []
    emitted: list[tuple[str, Any]] = []

    def legacy(config: AppConfig, payload: dict[str, Any], emit: Any | None = None) -> dict[str, Any]:  # noqa: ARG001
        calls.append(str(payload.get("session_id") or ""))
        if emit is not None:
            emit("legacy", {"session_id": payload.get("session_id")})
        return {"answer": "ok", "agent": payload.get("agent"), "sources": [], "context": ""}

    graph_runtime_module._GRAPH_CACHE.clear()
    with tempfile.TemporaryDirectory() as tmp:
        config = make_temp_config(Path(tmp))
        dispatch_agent_message_graph(
            config,
            {"session_id": "cache-a"},
            from_agent="User",
            content="hello",
            to_agent="MCagent",
            conversation_id="cache-a",
            legacy_delivery=legacy,
        )
        dispatch_agent_message_graph(
            config,
            {"session_id": "cache-b"},
            from_agent="User",
            content="hello again",
            to_agent="MCagent",
            conversation_id="cache-b",
            legacy_delivery=legacy,
        )
        assert_true("non_streaming_cache_one_graph", len(graph_runtime_module._GRAPH_CACHE) == 1, str(graph_runtime_module._GRAPH_CACHE))
        dispatch_agent_message_graph(
            config,
            {"session_id": "stream-c"},
            from_agent="User",
            content="stream",
            to_agent="MCagent",
            conversation_id="stream-c",
            legacy_delivery=legacy,
            emit=lambda event, data: emitted.append((event, data)),
        )
    assert_true("all_calls_delivered", calls == ["cache-a", "cache-b", "stream-c"], str(calls))
    assert_true("stream_emit_observed", any(event == "legacy" for event, _data in emitted), str(emitted))
    assert_true("stream_graph_not_cached", len(graph_runtime_module._GRAPH_CACHE) == 1, str(graph_runtime_module._GRAPH_CACHE))


def main() -> int:
    test_conversation_graph_routes_only_by_message_target()
    test_conversation_graph_can_dispatch_to_crawler_node()
    test_non_streaming_graph_reuses_checkpointed_runtime_without_reusing_emit()
    print("LANGGRAPH RUNTIME SCENARIOS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
