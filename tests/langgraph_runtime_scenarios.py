from __future__ import annotations

import json
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


def test_crawler_task_accounting_inserts_archive_internal_followup() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        export_dir = str(Path(tmp) / "downloaded_archive")
        result = {
            "returncode": 0,
            "manifest_stats": {"records": 1, "downloads": 1},
            "export_dir": export_dir,
        }
        plan: dict[str, Any] = {"delivery_target": "MCagent/RAG"}
        tasks = [{"source": "modpack_download", "query": "public archive"}]
        update = web_server._apply_crawler_task_accounting(
            result=result,
            task_source="modpack_download",
            task_payload={"query": "public archive"},
            question="public archive",
            payload={},
            plan=plan,
            tasks=tasks,
            index=1,
            max_total_tasks=4,
            result_accounting=web_server.CrawlerResultAccountingService(),
        )
    assert_true("accounting_success", update.get("success_delta") == 1, str(update))
    assert_true("accounting_needs_ingest", update.get("needs_ingest") is True, str(update))
    assert_true("accounting_export_dir", update.get("accepted_export_dirs") == [export_dir], str(update))
    assert_true("accounting_followup_inserted", update.get("inserted_followup") is True, str(update))
    assert_true("internal_followup_source", tasks[1].get("source") == "modpack_internal", str(tasks))
    assert_true("objective_reflection_recorded", any(item.get("action") == "add_tasks" for item in plan.get("agent_reflections") or []), str(plan))


def test_crawler_task_accounting_does_not_duplicate_followup() -> None:
    result = {
        "returncode": 0,
        "manifest_stats": {"records": 1, "downloads": 1},
        "export_dir": "",
    }
    plan: dict[str, Any] = {"delivery_target": "MCagent/RAG"}
    tasks = [
        {"source": "modpack_download", "query": "public archive"},
        {"source": "modpack_internal", "query": "public archive"},
    ]
    update = web_server._apply_crawler_task_accounting(
        result=result,
        task_source="modpack_download",
        task_payload={"query": "public archive"},
        question="public archive",
        payload={},
        plan=plan,
        tasks=tasks,
        index=1,
        max_total_tasks=4,
        result_accounting=web_server.CrawlerResultAccountingService(),
    )
    assert_true("duplicate_followup_not_inserted", update.get("inserted_followup") is False, str(update))
    assert_true("tasks_unchanged", len(tasks) == 2, str(tasks))


def test_crawler_task_accounting_turns_archive_fetch_observation_into_download_followup() -> None:
    result = {
        "returncode": 1,
        "manifest_stats": {
            "records": 0,
            "skipped": 1,
            "archive_url_detected": True,
            "failure_reason": "URL points to a binary modpack archive.",
        },
        "export_dir": "",
    }
    plan: dict[str, Any] = {"delivery_target": "MCagent/RAG"}
    tasks = [{"source": "fetch_url", "query": "https://example.com/demo.mrpack"}]
    update = web_server._apply_crawler_task_accounting(
        result=result,
        task_source="fetch_url",
        task_payload={"query": "https://example.com/demo.mrpack"},
        question="download demo",
        payload={},
        plan=plan,
        tasks=tasks,
        index=1,
        max_total_tasks=4,
        result_accounting=web_server.CrawlerResultAccountingService(),
    )
    assert_true("archive_fetch_candidate", update.get("candidate_delta") == 1, str(update))
    assert_true("archive_fetch_not_failure", update.get("failure_delta") == 0, str(update))
    assert_true("download_followup_inserted", update.get("inserted_followup") is True, str(update))
    assert_true("download_followup_source", tasks[1].get("source") == "modpack_download", str(tasks))


def test_crawler_task_step_blocks_empty_query_before_tool_execution() -> None:
    class FakeJob:
        id = "empty-query-step"
        result: dict[str, Any] | None = None
        status = "queued"
        summary = ""
        error = None
        created_at = 0.0
        started_at = None
        ended_at = None
        stop_requested = False

    with tempfile.TemporaryDirectory() as tmp:
        plan: dict[str, Any] = {}
        tasks = [{"source": "web_discovery", "query": "", "reason": "missing query"}]
        task_results: list[dict[str, Any]] = []
        step = web_server._execute_crawler_task_step(
            job=FakeJob(),
            config=make_temp_config(Path(tmp)),
            payload={},
            task=tasks[0],
            question="",
            plan=plan,
            tasks=tasks,
            index=1,
            task_results=task_results,
            session_summary=None,
            artifact_refs=web_server.ArtifactReferenceService(),
            task_preparation=web_server.CrawlerTaskPreparationService(),
            result_accounting=web_server.CrawlerResultAccountingService(),
            job_progress=web_server.CrawlerJobProgressService(),
            max_total_tasks=4,
        )
    assert_true("empty_query_continue", step.get("continue_loop") is True, str(step))
    assert_true("empty_query_failure", step.get("failure_delta") == 1, str(step))
    assert_true("empty_query_bad_streak", step.get("bad_streak_delta") == 1, str(step))
    assert_true("empty_query_result_recorded", len(task_results) == 1, str(task_results))
    assert_true("empty_query_no_accounting_success", not step.get("success_delta"), str(step))


def test_crawler_task_step_executes_command_and_records_accounting() -> None:
    class FakeJob:
        id = "command-step"
        result: dict[str, Any] | None = None
        status = "queued"
        summary = ""
        error = None
        created_at = 0.0
        started_at = None
        ended_at = None
        stop_requested = False

    original_command = web_server._run_crawler_command
    try:
        export_dir_holder: dict[str, str] = {}

        def fake_command(_command, _source, job=None):  # noqa: ANN001, ARG001
            export_dir = export_dir_holder["path"]
            return {
                "returncode": 0,
                "output": "collected",
                "export_dir": export_dir,
                "topic_validation": {"matched": True},
            }

        web_server._run_crawler_command = fake_command  # type: ignore[assignment]
        with tempfile.TemporaryDirectory() as tmp:
            export_dir = Path(tmp) / "export"
            export_dir.mkdir()
            (export_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "records": [{"title": "Downloaded Archive", "url": "https://example.com/unit.mrpack", "text": "download evidence"}],
                        "downloads": [{"url": "https://example.com/unit.mrpack", "path": str(export_dir / "unit.mrpack")}],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            export_dir_holder["path"] = str(export_dir)
            plan: dict[str, Any] = {"delivery_target": "MCagent/RAG"}
            tasks = [{"source": "modpack_download", "query": "https://example.com/unit.mrpack", "reason": "download"}]
            task_results: list[dict[str, Any]] = []
            step = web_server._execute_crawler_task_step(
                job=FakeJob(),
                config=make_temp_config(Path(tmp)),
                payload={},
                task=tasks[0],
                question="download unit archive",
                plan=plan,
                tasks=tasks,
                index=1,
                task_results=task_results,
                session_summary=None,
                artifact_refs=web_server.ArtifactReferenceService(),
                task_preparation=web_server.CrawlerTaskPreparationService(),
                result_accounting=web_server.CrawlerResultAccountingService(),
                job_progress=web_server.CrawlerJobProgressService(),
                max_total_tasks=4,
            )
    finally:
        web_server._run_crawler_command = original_command  # type: ignore[assignment]
    assert_true("step_not_blocked", step.get("continue_loop") is False, str(step))
    assert_true("step_success", step.get("success_delta") == 1, str(step))
    assert_true("step_result_recorded", len(task_results) == 1, str(task_results))
    assert_true("step_observation_recorded", isinstance(task_results[0].get("observation"), dict), str(task_results))
    assert_true("step_query_recorded", task_results[0].get("query") == "https://example.com/unit.mrpack", str(task_results))


def test_crawler_task_step_ignores_unbacked_tool_record_claims() -> None:
    class FakeJob:
        id = "unbacked-command-step"
        result: dict[str, Any] | None = None
        status = "queued"
        summary = ""
        error = None
        created_at = 0.0
        started_at = None
        ended_at = None
        stop_requested = False

    original_command = web_server._run_crawler_command
    try:
        web_server._run_crawler_command = lambda _command, _source, job=None: {  # type: ignore[assignment]
            "returncode": 0,
            "output": "collected",
            "export_dir": "",
            "manifest_stats": {"records": 1},
            "topic_validation": {"matched": True},
        }
        with tempfile.TemporaryDirectory() as tmp:
            plan: dict[str, Any] = {"delivery_target": "MCagent/RAG"}
            tasks = [{"source": "web_discovery", "query": "unit query", "reason": "collect"}]
            task_results: list[dict[str, Any]] = []
            step = web_server._execute_crawler_task_step(
                job=FakeJob(),
                config=make_temp_config(Path(tmp)),
                payload={},
                task=tasks[0],
                question="unit query",
                plan=plan,
                tasks=tasks,
                index=1,
                task_results=task_results,
                session_summary=None,
                artifact_refs=web_server.ArtifactReferenceService(),
                task_preparation=web_server.CrawlerTaskPreparationService(),
                result_accounting=web_server.CrawlerResultAccountingService(),
                job_progress=web_server.CrawlerJobProgressService(),
                max_total_tasks=4,
            )
    finally:
        web_server._run_crawler_command = original_command  # type: ignore[assignment]
    assert_true("step_not_blocked", step.get("continue_loop") is False, str(step))
    assert_true("unbacked_claim_not_success", step.get("success_delta") == 0, str(step))
    assert_true("unbacked_claim_failure", step.get("failure_delta") == 1, str(step))
    assert_true("step_result_recorded", len(task_results) == 1, str(task_results))
    assert_true("step_observation_recorded", isinstance(task_results[0].get("observation"), dict), str(task_results))
    assert_true("step_query_recorded", task_results[0].get("query") == "unit query", str(task_results))


def test_crawler_loop_control_finishes_after_rag_success_checkpoint() -> None:
    class FakeJob:
        id = "loop-finish"
        result: dict[str, Any] | None = None
        status = "queued"
        summary = ""
        error = None
        created_at = 0.0
        started_at = None
        ended_at = None
        stop_requested = False

    with tempfile.TemporaryDirectory() as tmp:
        plan: dict[str, Any] = {"delivery_target": "MCagent/RAG"}
        task_results = [
            {
                "source": "web_discovery",
                "returncode": 0,
                "manifest_stats": {"records": 1, "usable_records": 1, "empty_records": 0},
                "topic_validation": {"matched": True},
            }
            for _index in range(4)
        ]
        loop_update = web_server._apply_crawler_loop_control_after_task(
            job=FakeJob(),
            config=make_temp_config(Path(tmp)),
            payload={},
            source="planner",
            question="unit question",
            plan=plan,
            tasks=[{"source": "web_discovery", "query": "unit question"}],
            task_results=task_results,
            index=1,
            success_count=1,
            candidate_count=0,
            bad_streak=0,
            replan_count=0,
            max_replans=2,
            max_total_tasks=4,
            loop_control=web_server.CrawlerLoopControlService(),
            job_progress=web_server.CrawlerJobProgressService(),
        )
    assert_true("loop_finish_action", loop_update.get("action") == "finish", str(loop_update))
    assert_true("loop_finish_reason", "usable evidence" in str(plan.get("agent_finish_reason") or ""), str(plan))
    assert_true("loop_finish_reflection", any(item.get("action") == "finish" for item in plan.get("agent_reflections") or []), str(plan))


def test_direct_answer_route_helper_does_not_execute_unselected_delegate() -> None:
    class FakeRun:
        original_question = "ask crawler to collect later"
        question = original_question
        agent = "crawler_agent"
        model = "fake"
        temperature = 0.0
        max_tokens = 100
        is_streaming = False

        def __init__(self) -> None:
            self.config = None
            self.trace = []

        def add_trace(self, stage, status, detail=None):  # noqa: ANN001
            item = {"stage": stage, "status": status, "detail": detail}
            self.trace.append(item)
            return item

        def emit_delta(self, text: str) -> None:
            raise AssertionError(text)

        def response(self, payload: dict[str, Any]) -> dict[str, Any]:
            payload["trace"] = self.trace
            return payload

    original_review = web_server._tool_route_completeness_review
    try:
        web_server._tool_route_completeness_review = lambda *_args, **_kwargs: {  # type: ignore[assignment]
            "missing_side_effect": True,
            "tool": "delegate_crawler",
            "action": "execute_selected_tool",
            "collection_target": "collect public data",
        }
        run = FakeRun()
        executor = web_server.AgentToolExecutor(
            generate_direct_answer=lambda *_args, **_kwargs: "direct answer only",
            generate_direct_answer_stream=lambda *_args, **_kwargs: "direct answer only",
            status_answer=lambda _config: {"answer": "status"},
        )
        result = web_server._handle_direct_answer_route(
            config=make_temp_config(Path(tempfile.gettempdir())),
            agent="crawler_agent",
            model="fake",
            original_question=run.original_question,
            question=run.question,
            tool_decision={"tool": "direct_answer", "collection_target": "collect public data"},
            route_confirmation={},
            action_plan=[],
            executor=executor,
            run=run,
            session_summary={},
            add_trace=run.add_trace,
        )
    finally:
        web_server._tool_route_completeness_review = original_review  # type: ignore[assignment]
    statuses = [(item.get("stage"), item.get("status")) for item in result.get("trace") or []]
    assert_true("direct_answer_returned", result.get("answer") == "direct answer only", str(result))
    assert_true("missing_side_effect_visible", ("plan", "route_completeness_gap") in statuses, str(statuses))
    assert_true("delegate_not_executed", ("decide", "direct_answer_missing_side_effect_not_executed") in statuses, str(statuses))
    assert_true("no_job", not result.get("job"), str(result))


def main() -> int:
    test_conversation_graph_routes_only_by_message_target()
    test_conversation_graph_can_dispatch_to_crawler_node()
    test_non_streaming_graph_reuses_checkpointed_runtime_without_reusing_emit()
    test_agent_subgraphs_load_session_memory_context()
    test_crawler_background_job_enters_langgraph_runtime()
    test_crawler_job_plan_preparation_is_objective_and_reusable()
    test_crawler_task_preparation_routes_archive_urls_objectively()
    test_crawler_task_result_metadata_is_recorded_objectively()
    test_crawler_task_accounting_inserts_archive_internal_followup()
    test_crawler_task_accounting_does_not_duplicate_followup()
    test_crawler_task_accounting_turns_archive_fetch_observation_into_download_followup()
    test_crawler_task_step_blocks_empty_query_before_tool_execution()
    test_crawler_task_step_executes_command_and_records_accounting()
    test_crawler_task_step_ignores_unbacked_tool_record_claims()
    test_crawler_loop_control_finishes_after_rag_success_checkpoint()
    test_direct_answer_route_helper_does_not_execute_unselected_delegate()
    print("LANGGRAPH RUNTIME SCENARIOS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
