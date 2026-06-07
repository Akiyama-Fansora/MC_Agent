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
from mcagent.graphs.crawler_job import run_crawler_job_graph  # noqa: E402
from mcagent.session_state import DEFAULT_SESSION_STORE  # noqa: E402
import mcagent.web_server as web_server  # noqa: E402


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
    assert_true("mcagent_prepare_local_retrieval_node", "mcagent.prepare_local_retrieval" in agent_runtime.get("visited_nodes", []), str(agent_runtime))
    boundary = agent_runtime.get("tool_boundary") or {}
    assert_true("mcagent_local_only", "local_rag" in boundary.get("allowed_capability_groups", []), str(boundary))
    assert_true("mcagent_blocks_web", "web_search" in boundary.get("blocked_capability_groups", []), str(boundary))
    selected_groups = agent_runtime.get("selected_tool_groups") or {}
    local_tools = set(selected_groups.get("default_tools") or [])
    assert_true("mcagent_default_local_group", selected_groups.get("default_groups") == ["local"], str(selected_groups))
    assert_true("mcagent_local_tools_include_rag", "local_rag_search" in local_tools, str(selected_groups))
    assert_true("mcagent_local_tools_include_message_handoff", "delegate_crawler" in local_tools, str(selected_groups))
    assert_true("mcagent_local_tools_exclude_crawler_web", not {"web_discovery", "fetch_url", "playwright", "browser_collect", "modpack_download"} & local_tools, str(selected_groups))
    retrieval_contract = agent_runtime.get("retrieval_contract") or {}
    assert_true("mcagent_retrieval_local_sources", "local_rag" in retrieval_contract.get("allowed_evidence_sources", []), str(retrieval_contract))
    assert_true("mcagent_retrieval_blocks_public_web", "public_web" in retrieval_contract.get("blocked_evidence_sources", []), str(retrieval_contract))


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
    assert_true("crawler_prepare_mission_contract_node", "crawler.prepare_mission_contract" in agent_runtime.get("visited_nodes", []), str(agent_runtime))
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
    mission_contract = agent_runtime.get("mission_contract") or {}
    assert_true("crawler_mission_delivery", mission_contract.get("delivery_target") == "MCagent/RAG", str(mission_contract))
    assert_true("crawler_mission_owner", mission_contract.get("decision_owner") == "CrawlerAgent LLM", str(mission_contract))


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


def test_agent_subgraphs_load_session_memory_context() -> None:
    session_id = "graph-memory-context"
    DEFAULT_SESSION_STORE.delete(session_id)
    DEFAULT_SESSION_STORE.append_turn(session_id, {"question": "first question", "answer": "first answer"})

    def legacy(config: AppConfig, payload: dict[str, Any], emit: Any | None = None) -> dict[str, Any]:  # noqa: ARG001
        return {"answer": "ok", "agent": payload.get("agent"), "sources": [], "context": ""}

    with tempfile.TemporaryDirectory() as tmp:
        mc_result = dispatch_agent_message_graph(
            make_temp_config(Path(tmp)),
            {"session_id": session_id},
            from_agent="User",
            content="use memory",
            to_agent="MCagent",
            conversation_id=session_id,
            legacy_delivery=legacy,
        )
        crawler_result = dispatch_agent_message_graph(
            make_temp_config(Path(tmp)),
            {"session_id": session_id},
            from_agent="User",
            content="use memory too",
            to_agent="CrawlerAgent",
            conversation_id=session_id,
            legacy_delivery=legacy,
        )
    mc_memory = (mc_result.get("agent_graph_runtime") or {}).get("memory_context") or {}
    crawler_memory = (crawler_result.get("agent_graph_runtime") or {}).get("memory_context") or {}
    assert_true("mcagent_memory_session", mc_memory.get("session_id") == session_id, str(mc_memory))
    assert_true("mcagent_memory_turn_count", mc_memory.get("turn_count") == 1, str(mc_memory))
    assert_true("crawler_memory_session", crawler_memory.get("session_id") == session_id, str(crawler_memory))
    assert_true("crawler_memory_turn_count", crawler_memory.get("turn_count") == 1, str(crawler_memory))
    DEFAULT_SESSION_STORE.delete(session_id)


def test_crawler_background_job_enters_langgraph_runtime() -> None:
    class FakeJob:
        id = "job-graph-test"
        result: dict[str, Any] | None = None

    calls: list[dict[str, Any]] = []

    def legacy_loop(job: FakeJob, payload: dict[str, Any], config: AppConfig) -> None:  # noqa: ARG001
        calls.append(dict(payload))
        job.result = {"legacy_loop": "ran"}

    with tempfile.TemporaryDirectory() as tmp:
        job = FakeJob()
        run_crawler_job_graph(
            make_temp_config(Path(tmp)),
            job,
            {"session_id": "crawler-job-graph", "source": "planner", "delivery_target": "MCagent/RAG", "agent_message": {"ok": True}},
            legacy_loop=legacy_loop,
        )
    assert_true("legacy_loop_called", len(calls) == 1, str(calls))
    runtime = (job.result or {}).get("crawler_job_graph_runtime") or {}
    assert_true("job_graph_runtime", runtime.get("graph") == "CrawlerJobGraph", str(runtime))
    assert_true("job_graph_receive", "crawler_job.receive" in runtime.get("visited_nodes", []), str(runtime))
    assert_true("job_graph_legacy_loop", "crawler_job.legacy_loop" in runtime.get("visited_nodes", []), str(runtime))
    contract = runtime.get("job_contract") or {}
    assert_true("job_graph_contract_delivery", contract.get("delivery_target") == "MCagent/RAG", str(contract))
    assert_true("job_graph_contract_message", contract.get("has_agent_message") is True, str(contract))
    assert_true("job_graph_contract_owner", contract.get("decision_owner") == "CrawlerAgent LLM", str(contract))


def test_crawler_job_plan_preparation_is_objective_and_reusable() -> None:
    class FakeJob:
        id = "prepare-plan"
        result: dict[str, Any] | None = {"reuse_signature": "reuse-1", "requested_by": "unit"}
        stop_requested = False
        ended_at = None

    original_plan = web_server._plan_crawler_with_job_timeout
    try:
        web_server._plan_crawler_with_job_timeout = lambda *_args, **_kwargs: {  # type: ignore[assignment]
            "topic": "unit topic",
            "tasks": [{"source": "web_discovery", "query": "unit query"}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            job = FakeJob()
            prepared = web_server._prepare_crawler_job_plan(
                job=job,
                payload={"source": "planner", "session_summary": {"delivery_target": "human"}},
                config=make_temp_config(Path(tmp)),
                source="planner",
                question="unit question",
                job_setup=web_server.CrawlerJobSetupService(),
                job_progress=web_server.CrawlerJobProgressService(),
            )
    finally:
        web_server._plan_crawler_with_job_timeout = original_plan  # type: ignore[assignment]
    assert_true("prepared_not_stopped", prepared.get("stopped") is False, str(prepared))
    assert_true("prepared_tasks", prepared.get("tasks") == [{"source": "web_discovery", "query": "unit query"}], str(prepared))
    assert_true("prepared_session_summary", prepared.get("session_summary") == {"delivery_target": "human"}, str(prepared))
    assert_true("job_planned_result_keeps_reuse", (job.result or {}).get("reuse_signature") == "reuse-1", str(job.result))

    single = web_server._prepare_crawler_job_plan(
        job=FakeJob(),
        payload={"source": "fetch_url", "query": "https://example.com"},
        config=make_temp_config(Path(tempfile.gettempdir())),
        source="fetch_url",
        question="fallback question",
        job_setup=web_server.CrawlerJobSetupService(),
        job_progress=web_server.CrawlerJobProgressService(),
    )
    assert_true("single_source_tasks", single.get("tasks") == [{"source": "fetch_url", "query": "https://example.com", "reason": "single source request"}], str(single))


def test_crawler_task_preparation_routes_archive_urls_objectively() -> None:
    plan: dict[str, Any] = {"topic": "archive test"}
    prepared = web_server._prepare_crawler_task_execution(
        payload={"session_id": "task-prep"},
        task={"source": "fetch_url", "query": "https://example.com/demo.mrpack", "reason": "download archive"},
        question="download archive",
        plan=plan,
        current_index=1,
        artifact_refs=web_server.ArtifactReferenceService(),
        task_preparation=web_server.CrawlerTaskPreparationService(),
    )
    assert_true("archive_routed_to_download", prepared.get("task_source") == "modpack_download", str(prepared))
    assert_true("archive_payload_source", prepared.get("task_payload", {}).get("source") == "modpack_download", str(prepared))
    reflections = plan.get("agent_reflections") or []
    assert_true("archive_reflection_recorded", any(item.get("action") == "route_archive_url_to_modpack_download" for item in reflections), str(reflections))


def test_crawler_task_result_metadata_is_recorded_objectively() -> None:
    result = {"returncode": 1, "output": "network error", "export_dir": ""}
    plan: dict[str, Any] = {}
    records = web_server._record_crawler_task_result_metadata(
        result=result,
        task={"reason": "unit reason"},
        task_source="fetch_url",
        task_payload={"query": "https://example.com/missing"},
        question="unit question",
        plan=plan,
        result_index=1,
        artifact_refs=web_server.ArtifactReferenceService(),
    )
    assert_true("metadata_records_zero", records == 0, str(result))
    assert_true("metadata_query", result.get("query") == "https://example.com/missing", str(result))
    assert_true("metadata_reason", result.get("reason") == "unit reason", str(result))
    stats = result.get("manifest_stats") or {}
    assert_true("metadata_inline_failure_stats", stats == {"records": 0, "skipped": 0, "errors": 0}, str(result))


def main() -> int:
    test_conversation_graph_routes_only_by_message_target()
    test_conversation_graph_can_dispatch_to_crawler_node()
    test_non_streaming_graph_reuses_checkpointed_runtime_without_reusing_emit()
    test_agent_subgraphs_load_session_memory_context()
    test_crawler_background_job_enters_langgraph_runtime()
    test_crawler_job_plan_preparation_is_objective_and_reusable()
    test_crawler_task_preparation_routes_archive_urls_objectively()
    test_crawler_task_result_metadata_is_recorded_objectively()
    print("LANGGRAPH RUNTIME SCENARIOS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
