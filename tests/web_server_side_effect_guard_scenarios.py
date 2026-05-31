from __future__ import annotations

from pathlib import Path
import json
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import mcagent.web_server as web_server  # noqa: E402
from mcagent.config import AppConfig, ChunkingConfig, EmbeddingConfig, OllamaConfig, PathsConfig, RetrievalConfig  # noqa: E402
from mcagent.schema import SearchResult  # noqa: E402


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
        ollama=OllamaConfig(model="fake-model"),
    )


def assert_equal(name: str, actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"{name}: expected {expected!r}, got {actual!r}")


def assert_true(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name}: {detail}")


def test_direct_crawler_no_save_url_uses_temporary_extract_boundary() -> None:
    question = "总结一下 https://example.com 的内容给我，不用保存到本地"
    assert_true(
        "temporary_boundary",
        web_server._should_use_temporary_extract_without_persistence("crawler_agent", question, question, "human"),
    )
    assert_true(
        "mcagent_not_forced",
        not web_server._should_use_temporary_extract_without_persistence("mcagent_rag", question, question, "human"),
    )
    assert_true(
        "rag_delivery_not_forced",
        not web_server._should_use_temporary_extract_without_persistence("crawler_agent", question, question, "MCagent/RAG"),
    )
    neutral_url_question = "总结一下 https://example.com 的内容给我"
    assert_true(
        "plain_url_summary_is_temporary_by_default",
        web_server._should_use_temporary_extract_without_persistence("crawler_agent", neutral_url_question, neutral_url_question, "human"),
    )
    save_question = r"读取 https://example.com 的内容并保存到 C:\tmp\example.md"
    assert_true(
        "explicit_save_stays_persistent",
        not web_server._should_use_temporary_extract_without_persistence("crawler_agent", save_question, save_question, "human"),
    )


class FakeClient:
    def chat(self, messages: list[dict[str, Any]], *, temperature: float, max_tokens: int) -> str:  # noqa: ARG002
        return '{"handoff_brief":"调用关系：MCagent 将用户请求转交给 CrawlerAgent。","reason":"fake"}'


class FailingClient:
    def chat(self, messages: list[dict[str, Any]], *, temperature: float, max_tokens: int | None) -> str:  # noqa: ARG002
        raise RuntimeError("primary profile failed")


def test_grounded_answer_does_not_fallback_to_ollama_after_profile_error() -> None:
    config = make_temp_config(Path(tempfile.mkdtemp(prefix="mcagent-no-ollama-fallback-")))
    original_selector = web_server._selected_llm_client
    original_ollama = web_server.OllamaOpenAIClient
    try:
        web_server._selected_llm_client = lambda *_args, **_kwargs: (FailingClient(), "DeepSeek test")  # type: ignore[assignment]
        web_server.OllamaOpenAIClient = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected Ollama fallback"))  # type: ignore[assignment]
        answer, _context = web_server._generate_grounded_answer(
            config,
            "question",
            [],
            "profile:deepseek-template",
            0.0,
            128,
            context_override="evidence",
        )
    finally:
        shutil.rmtree(config.paths.project_root, ignore_errors=True)
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        web_server.OllamaOpenAIClient = original_ollama  # type: ignore[assignment]
    assert_true("reports_primary_error", "primary profile failed" in answer, answer)
    assert_true("no_auto_ollama_note", "已自动降级" not in answer and "未自动降级" in answer, answer)


def test_direct_user_handoff_brief_rejects_wrong_mcagent_identity() -> None:
    original_selector = web_server._selected_llm_client
    web_server._selected_llm_client = lambda *_args, **_kwargs: (FakeClient(), "fake")  # type: ignore[assignment]
    try:
        brief, reason = web_server._build_delegate_handoff_brief(
            object(),  # type: ignore[arg-type]
            model="fake",
            original_question="Crawler 直接采集公开网页",
            collection_target="采集公开网页",
            session_summary={},
            requested_by="user",
            delivery_target="human",
        )
    finally:
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]

    assert_true("uses_user_identity", "Requested by: user" in brief or "From: user" in brief)
    assert_true("no_wrong_mcagent_transfer", "MCagent 将用户请求转交" not in brief)
    assert_equal("reason", reason, "LLM handoff brief conflicted with requested_by=user; used identity-safe fallback.")


class SequencedClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls: list[list[dict[str, Any]]] = []

    def chat(self, messages: list[dict[str, Any]], *, temperature: float, max_tokens: int) -> str:  # noqa: ARG002
        self.calls.append(messages)
        if not self.responses:
            return "ok"
        return self.responses.pop(0)


def test_direct_crawler_delegate_choice_is_corrected_to_temporary_extract() -> None:
    tmp = tempfile.TemporaryDirectory()
    fake_client = SequencedClient(
        [
            '{"tool":"delegate_crawler","reason":"mistaken persistent route","collection_target":"总结 https://example.com 页面内容","delivery_target":"human"}',
            '{"proceed":true,"tool":"delegate_crawler","reason":"confirm mistaken route"}',
            '{"proceed":true,"tool":"temporary_extract","reason":"temporary extraction is allowed"}',
            "Example page summary.",
        ]
    )
    original_selector = web_server._selected_llm_client
    original_fetch_text = web_server.CrawlerTemporaryExtractService.fetch_text
    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake_client, "fake")  # type: ignore[assignment]
    web_server.CrawlerTemporaryExtractService.fetch_text = (  # type: ignore[assignment]
        lambda self, url, *, fetch=None: ("Example", "Example body text. " * 20, "text/html", 200)
    )
    try:
        result = web_server._chat_impl(
            make_temp_config(Path(tmp.name)),
            {
                "agent": "crawler_agent",
                "question": "总结一下 https://example.com 的内容给我，不用保存到本地",
                "session_id": "direct-crawler-side-effect-test",
                "model": "fake-model",
            },
        )
    finally:
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        web_server.CrawlerTemporaryExtractService.fetch_text = original_fetch_text  # type: ignore[assignment]
        tmp.cleanup()

    statuses = [(step["stage"], step["status"]) for step in result.get("trace", [])]
    assert_true("boundary_trace", ("decide", "side_effect_boundary_corrected") in statuses)
    assert_true("temporary_result", result.get("temporary_extract", {}).get("saved_to_local") is False)
    assert_equal("agent_identity", result.get("agent"), "crawler_agent")
    assert_true("no_background_job", "job" not in result)


def test_direct_crawler_mcagent_gap_request_forces_planned_workflow() -> None:
    decision = {
        "tool": "direct_answer",
        "reason": "CrawlerAgent cannot ask MCagent directly.",
        "collection_target": "问下MCAgent乌托邦整合包还缺哪些东西 你去网上找补给他",
        "delivery_target": "human",
    }
    assert_true(
        "forces_inter_agent_workflow",
        web_server._should_force_crawler_mcagent_gap_workflow(
            "crawler_agent",
            "问下MCAgent乌托邦整合包还缺哪些东西 你去网上找补给他",
            "direct_answer",
            decision,
        ),
    )
    assert_true(
        "simple_direct_answer_not_forced",
        not web_server._should_force_crawler_mcagent_gap_workflow(
            "crawler_agent",
            "你好",
            "direct_answer",
            {"tool": "direct_answer", "reason": "greeting"},
        ),
    )
    plan = web_server._default_mcagent_gap_action_plan()
    assert_equal("plan_first_tool", plan[0]["tool"], "mcagent_context")
    assert_equal("plan_second_tool", plan[1]["tool"], "delegate_crawler")


def test_direct_crawler_delegate_gap_request_is_rewritten_to_context_workflow() -> None:
    decision = {
        "tool": "delegate_crawler",
        "reason": "collect after checking MCagent gaps",
        "collection_target": "先检查MCagent本地资料中关于乌托邦整合包缺失的内容，然后去网上找补给他",
        "delivery_target": "MCagent/RAG",
    }
    assert_true(
        "forces_delegate_to_planned_workflow",
        web_server._should_force_crawler_mcagent_gap_workflow(
            "crawler_agent",
            "问下MCAgent乌托邦整合包还缺哪些东西 你去网上找补给他",
            "delegate_crawler",
            decision,
        ),
    )


def test_direct_crawler_router_error_gap_request_recovers_to_context_workflow() -> None:
    decision = {
        "tool": "router_error",
        "reason": "Agent tool selector failed JSON parsing",
        "collection_target": "问下MCAgent乌托邦整合包还缺哪些东西 你去网上找补给他",
        "delivery_target": "",
    }
    assert_true(
        "forces_router_error_to_planned_workflow",
        web_server._should_force_crawler_mcagent_gap_workflow(
            "crawler_agent",
            "问下MCAgent乌托邦整合包还缺哪些东西 你去网上找补给他",
            "router_error",
            decision,
        ),
    )


def test_mcagent_context_focus_expands_minecraft_utopia_aliases() -> None:
    focus = web_server._mcagent_context_focus("问下MCAgent乌托邦整合包还缺哪些东西，你去网上找补给他")
    assert_true("focus_keeps_user_topic", "乌托邦" in focus)
    assert_true("focus_adds_full_pack_name", "乌托邦探险之旅" in focus)
    assert_true("focus_adds_english_alias", "Utopian Journey" in focus)


def test_successful_mcagent_context_prunes_duplicate_pending_context_tasks() -> None:
    tasks = [
        {"source": "mcagent_context", "query": "乌托邦缺口"},
        {"source": "mcmod", "query": "乌托邦探险之旅"},
        {"source": "mcagent_context", "query": "乌托邦整合包"},
        {"source": "web_discovery", "query": "乌托邦攻略"},
    ]
    removed = web_server._prune_pending_mcagent_context_tasks_after_success(tasks, 1)
    assert_equal("removed_count", len(removed), 1)
    assert_equal("remaining_sources", [item["source"] for item in tasks], ["mcagent_context", "mcmod", "web_discovery"])


def test_successful_mcagent_context_filters_new_duplicate_context_tasks() -> None:
    task_results = [
        {
            "source": "mcagent_context",
            "returncode": 0,
            "manifest_stats": {"records": 1},
        }
    ]
    new_tasks = [
        {"source": "mcagent_context", "query": "repeat"},
        {"source": "web_discovery", "query": "乌托邦探险之旅"},
    ]
    filtered = web_server._drop_duplicate_mcagent_context_tasks(new_tasks, task_results)
    assert_equal("remaining_sources", [item["source"] for item in filtered], ["web_discovery"])


def test_runtime_status_request_bypasses_llm_router() -> None:
    tmp = tempfile.TemporaryDirectory()
    original_selector = web_server._selected_llm_client
    web_server._selected_llm_client = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("status must not call LLM"))  # type: ignore[assignment]
    try:
        result = web_server._chat_impl(
            make_temp_config(Path(tmp.name)),
            {"agent": "mcagent_rag", "question": "状态", "session_id": "runtime-status-fast-path"},
        )
    finally:
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        tmp.cleanup()

    statuses = [(step["stage"], step["status"]) for step in result.get("trace", [])]
    assert_true("status_tool_selected", ("decide", "tool_selected") in statuses)
    assert_true("status_answer", "本地库" in str(result.get("answer") or "") and bool(result.get("status")))


def test_mcagent_gap_delegation_overrides_human_delivery_to_rag() -> None:
    tmp = tempfile.TemporaryDirectory()
    question = "\u73b0\u5728\u4e4c\u6258\u90a6\u6574\u5408\u5305\u4f60\u672c\u5730\u8fd8\u7f3a\u54ea\u4e9b\u8d44\u6599\uff0c\u5217\u51fa\u6765\uff0c\u7136\u540e\u8ba9 Crawler \u53bb\u8865\u5145\u3002"
    fake_client = SequencedClient(
        [
            json.dumps(
                {
                    "tool": "delegate_crawler",
                    "reason": "needs Crawler to collect missing local knowledge",
                    "collection_target": question,
                    "delivery_target": "human",
                },
                ensure_ascii=False,
            ),
            '{"proceed":true,"tool":"delegate_crawler","reason":"confirmed"}',
            '{"handoff_brief":"MCagent delegates missing Utopia material to CrawlerAgent for RAG ingestion.","reason":"handoff"}',
        ]
    )
    original_selector = web_server._selected_llm_client
    original_delegate = web_server._delegate_crawler_for_missing_data
    calls: list[dict[str, Any]] = []

    def fake_delegate(config: AppConfig, payload: dict[str, Any], delegated_question: str, plan: dict[str, Any] | None = None):  # noqa: ARG001
        calls.append({"payload": payload, "question": delegated_question, "plan": plan})
        job = web_server.Job(id="fake-mcagent-gap-job", kind="crawler", title=delegated_question, status="queued", summary="queued")
        job.result = {"plan": {"topic": delegated_question, "delivery_target": payload.get("delivery_target")}}
        return job, True

    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake_client, "fake")  # type: ignore[assignment]
    web_server._delegate_crawler_for_missing_data = fake_delegate  # type: ignore[assignment]
    try:
        result = web_server._chat_impl(
            make_temp_config(Path(tmp.name)),
            {
                "agent": "mcagent_rag",
                "question": question,
                "session_id": "mcagent-gap-human-delivery-correction",
                "model": "fake-model",
            },
        )
    finally:
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        web_server._delegate_crawler_for_missing_data = original_delegate  # type: ignore[assignment]
        tmp.cleanup()

    assert_true("delegated", bool(calls))
    assert_equal("requested_by", result.get("delegation", {}).get("requested_by"), "user_via_mcagent")
    assert_equal("delivery_target", result.get("delegation", {}).get("delivery_target"), "MCagent/RAG")
    assert_equal("payload_delivery", calls[0]["payload"].get("delivery_target"), "MCagent/RAG")


def test_explicit_mcagent_to_crawler_handoff_starts_job_before_router() -> None:
    tmp = tempfile.TemporaryDirectory()
    question = "\u8bf7\u5148\u68c0\u67e5\u672c\u5730\u8d44\u6599\u91cc\u4e4c\u6258\u90a6\u63a2\u9669\u4e4b\u65c5 / Utopian Journey \u6574\u5408\u5305\u8fd8\u7f3a\u54ea\u4e9b\u5185\u5bb9\uff0c\u7136\u540e\u8ba9 CrawlerAgent \u53bb\u7f51\u4e0a\u91c7\u96c6\u7f3a\u5931\u7684\u516c\u5f00\u8d44\u6599\u5e76\u5165\u5e93\u7ed9 MCagent/RAG \u4f7f\u7528\u3002"
    original_delegate = web_server._delegate_crawler_for_missing_data
    original_selector = web_server._selected_llm_client
    calls: list[dict[str, Any]] = []

    def fake_delegate(config: AppConfig, payload: dict[str, Any], delegated_question: str, plan: dict[str, Any] | None = None):  # noqa: ARG001
        calls.append({"payload": payload, "question": delegated_question, "plan": plan})
        job = web_server.Job(id="fake-fast-handoff-job", kind="crawler", title=delegated_question, status="queued", summary="queued")
        job.result = {"plan": {"topic": delegated_question, "delivery_target": payload.get("delivery_target")}}
        return job, True

    web_server._delegate_crawler_for_missing_data = fake_delegate  # type: ignore[assignment]
    web_server._selected_llm_client = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("router should not be called"))  # type: ignore[assignment]
    try:
        result = web_server._chat_impl(
            make_temp_config(Path(tmp.name)),
            {
                "agent": "mcagent_rag",
                "question": question,
                "session_id": "explicit-mcagent-crawler-fast-path",
                "model": "fake-model",
            },
        )
    finally:
        web_server._delegate_crawler_for_missing_data = original_delegate  # type: ignore[assignment]
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        tmp.cleanup()

    assert_true("delegated", bool(calls))
    assert_equal("requested_by", calls[0]["payload"].get("requested_by"), "user_via_mcagent")
    assert_equal("delivery_target", calls[0]["payload"].get("delivery_target"), "MCagent/RAG")
    assert_true("clean_target_keeps_alias", "乌托邦探险之旅 / Utopian Journey" in calls[0]["question"], calls[0]["question"])
    assert_true("clean_target_no_agent_damage", "Crawle ent" not in calls[0]["question"] and "给 / 使用" not in calls[0]["question"], calls[0]["question"])
    assert_true("has_job", result.get("job", {}).get("id") == "fake-fast-handoff-job")
    statuses = [(item.get("stage"), item.get("status")) for item in result.get("trace") or []]
    assert_true("fast_trace", ("delegate", "explicit_mcagent_handoff_fast_path") in statuses, str(statuses))


def test_mcagent_explicit_crawler_request_forces_planned_delegate() -> None:
    question = "现在乌托邦整合包你本地还缺哪些资料，列出来，然后让 Crawler 去补充。"
    decision = {
        "tool": "answer",
        "reason": "list gaps only",
        "collection_target": question,
        "delivery_target": "human",
    }
    assert_true(
        "force_planned_delegate",
        web_server._should_force_mcagent_planned_delegate("mcagent_rag", question, "answer", decision, []),
    )
    assert_true(
        "respect_no_crawler",
        not web_server._should_force_mcagent_planned_delegate(
            "mcagent_rag",
            "列出缺口，但不要交给 Crawler。",
            "answer",
            {"tool": "answer", "reason": "answer only", "collection_target": "", "delivery_target": "human"},
            [],
        ),
    )


def test_direct_crawler_mcagent_gap_request_delegates_when_local_empty() -> None:
    tmp = tempfile.TemporaryDirectory()
    fake_client = SequencedClient(
        [
            '{"tool":"direct_answer","reason":"CrawlerAgent cannot ask MCagent directly.","collection_target":"问下MCAgent乌托邦整合包还缺哪些东西 你去网上找补给他","delivery_target":"human"}',
            '{"proceed":true,"tool":"direct_answer","reason":"mistaken direct answer"}',
            '{"handoff_brief":"用户直接委托 CrawlerAgent：先参考 MCagent/RAG 空缺，再采集乌托邦整合包缺失资料。","reason":"handoff"}',
        ]
    )
    original_selector = web_server._selected_llm_client
    original_delegate = web_server._delegate_crawler_for_missing_data
    original_retrieve = web_server.RagRetrievalService.retrieve
    calls: list[dict[str, Any]] = []

    def fake_delegate(config: AppConfig, payload: dict[str, Any], question: str, plan: dict[str, Any] | None = None):  # noqa: ARG001
        calls.append({"payload": payload, "question": question, "plan": plan})
        job = web_server.Job(id="fake-crawler-job", kind="crawler", title=question, status="queued", summary="queued")
        job.result = {"plan": {"topic": question, "delivery_target": payload.get("delivery_target")}}
        return job, True

    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake_client, "fake")  # type: ignore[assignment]
    web_server._delegate_crawler_for_missing_data = fake_delegate  # type: ignore[assignment]
    def fail_if_route_reads_rag(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("direct Crawler mcagent_context+collection must run inside the Crawler job, not as chat-turn retrieval")

    web_server.RagRetrievalService.retrieve = fail_if_route_reads_rag  # type: ignore[assignment]
    try:
        result = web_server._chat_impl(
            make_temp_config(Path(tmp.name)),
            {
                "agent": "crawler_agent",
                "question": "问下MCAgent乌托邦整合包还缺哪些东西  你去网上找补给他",
                "session_id": "direct-crawler-mcagent-gap-test",
                "model": "fake-model",
            },
        )
    finally:
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        web_server._delegate_crawler_for_missing_data = original_delegate  # type: ignore[assignment]
        web_server.RagRetrievalService.retrieve = original_retrieve  # type: ignore[assignment]
        tmp.cleanup()

    statuses = [(step["stage"], step["status"]) for step in result.get("trace", [])]
    assert_true("inter_agent_correction_trace", ("decide", "inter_agent_workflow_corrected") in statuses)
    assert_true("delegated", bool(calls))
    assert_equal("requested_by", result.get("delegation", {}).get("requested_by"), "user")
    assert_equal("delivery_target", result.get("delegation", {}).get("delivery_target"), "MCagent/RAG")
    assert_true("mentions_topic", "乌托邦" in calls[0]["question"])
    assert_true("clean_collection_target", "CrawlerAgent 应" not in calls[0]["question"] and "用户原始目标" not in calls[0]["question"])
    summary = calls[0]["payload"].get("session_summary") or {}
    assert_true("planning_instruction_carried", "mcagent_context" in str(summary.get("planning_instruction") or ""))


def test_crawler_mcagent_context_with_collection_continues_to_delegate() -> None:
    tmp = tempfile.TemporaryDirectory()
    fake_client = SequencedClient(
        [
            '{"tool":"mcagent_context","reason":"inspect local gaps first","rag_focus":"乌托邦整合包缺口","collection_target":"问下MCAgent乌托邦整合包还缺哪些东西 你去网上找补给他","delivery_target":"MCagent/RAG","action_plan":[{"step":1,"tool":"mcagent_context","goal":"inspect local gaps"},{"step":2,"tool":"delegate_crawler","goal":"collect missing data"}]}',
            '{"proceed":true,"tool":"mcagent_context","reason":"context first"}',
            '{"handoff_brief":"用户直接委托 CrawlerAgent：根据 MCagent/RAG 缺口采集乌托邦整合包资料。","reason":"handoff"}',
        ]
    )
    original_selector = web_server._selected_llm_client
    original_delegate = web_server._delegate_crawler_for_missing_data
    original_retrieve = web_server.RagRetrievalService.retrieve
    calls: list[dict[str, Any]] = []

    def fake_delegate(config: AppConfig, payload: dict[str, Any], question: str, plan: dict[str, Any] | None = None):  # noqa: ARG001
        calls.append({"payload": payload, "question": question, "plan": plan})
        job = web_server.Job(id="fake-crawler-job-2", kind="crawler", title=question, status="queued", summary="queued")
        job.result = {"plan": {"topic": question, "delivery_target": payload.get("delivery_target")}}
        return job, True

    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake_client, "fake")  # type: ignore[assignment]
    web_server._delegate_crawler_for_missing_data = fake_delegate  # type: ignore[assignment]
    web_server.RagRetrievalService.retrieve = (  # type: ignore[assignment]
        lambda self, *args, **kwargs: SimpleNamespace(retrieval_plan=None, rough_results=[], selected=[])
    )
    try:
        result = web_server._chat_impl(
            make_temp_config(Path(tmp.name)),
            {
                "agent": "crawler_agent",
                "question": "问下MCAgent乌托邦整合包还缺哪些东西 你去网上找补给他",
                "session_id": "direct-crawler-mcagent-context-then-delegate-test",
                "model": "fake-model",
            },
        )
    finally:
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        web_server._delegate_crawler_for_missing_data = original_delegate  # type: ignore[assignment]
        web_server.RagRetrievalService.retrieve = original_retrieve  # type: ignore[assignment]
        tmp.cleanup()

    statuses = [(step["stage"], step["status"]) for step in result.get("trace", [])]
    assert_true("mcagent_context_trace", ("decide", "mcagent_context_selected") in statuses)
    assert_true("deferred_to_job", ("decide", "mcagent_context_deferred_to_crawler_job") in statuses)
    assert_true("delegated_after_context", bool(calls))
    assert_equal("delivery_target", result.get("delegation", {}).get("delivery_target"), "MCagent/RAG")
    assert_true("job_task_keeps_clean_topic", "乌托邦" in calls[0]["question"] and "mcagent_context" not in calls[0]["question"])
    summary = calls[0]["payload"].get("session_summary") or {}
    assert_true("planning_instruction_carried", "mcagent_context" in str(summary.get("planning_instruction") or ""))


def test_direct_crawler_delegate_choice_runs_as_crawler_context_workflow() -> None:
    tmp = tempfile.TemporaryDirectory()
    fake_client = SequencedClient(
        [
            '{"tool":"delegate_crawler","reason":"check gaps then collect","collection_target":"先检查MCagent本地资料中关于乌托邦整合包缺失的内容，然后去网上找补给他","delivery_target":"MCagent/RAG","action_plan":[{"step":1,"tool":"delegate_crawler","goal":"collect"}]}',
            '{"proceed":true,"tool":"delegate_crawler","reason":"confirmed"}',
            '{"handoff_brief":"用户直接委托 CrawlerAgent：先参考 MCagent/RAG 空缺，再采集乌托邦整合包缺失资料。","reason":"handoff"}',
        ]
    )
    original_selector = web_server._selected_llm_client
    original_delegate = web_server._delegate_crawler_for_missing_data
    original_retrieve = web_server.RagRetrievalService.retrieve
    calls: list[dict[str, Any]] = []

    def fake_delegate(config: AppConfig, payload: dict[str, Any], question: str, plan: dict[str, Any] | None = None):  # noqa: ARG001
        calls.append({"payload": payload, "question": question, "plan": plan})
        job = web_server.Job(id="fake-crawler-job-3", kind="crawler", title=question, status="queued", summary="queued")
        job.result = {"plan": {"topic": question, "delivery_target": payload.get("delivery_target")}}
        return job, True

    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake_client, "fake")  # type: ignore[assignment]
    web_server._delegate_crawler_for_missing_data = fake_delegate  # type: ignore[assignment]
    web_server.RagRetrievalService.retrieve = (  # type: ignore[assignment]
        lambda self, *args, **kwargs: SimpleNamespace(retrieval_plan=None, rough_results=[], selected=[])
    )
    try:
        result = web_server._chat_impl(
            make_temp_config(Path(tmp.name)),
            {
                "agent": "crawler_agent",
                "question": "问下MCAgent乌托邦整合包还缺哪些东西 你去网上找补给他",
                "session_id": "direct-crawler-delegate-to-context-workflow-test",
                "model": "fake-model",
            },
        )
    finally:
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        web_server._delegate_crawler_for_missing_data = original_delegate  # type: ignore[assignment]
        web_server.RagRetrievalService.retrieve = original_retrieve  # type: ignore[assignment]
        tmp.cleanup()

    statuses = [(step["stage"], step["status"]) for step in result.get("trace", [])]
    assert_true("inter_agent_correction_trace", ("decide", "inter_agent_workflow_corrected") in statuses)
    assert_true("delegated", bool(calls))
    assert_equal("agent_identity", result.get("agent"), "crawler_agent")
    assert_equal("requested_by", result.get("delegation", {}).get("requested_by"), "user")
    assert_equal("delivery_target", result.get("delegation", {}).get("delivery_target"), "MCagent/RAG")
    assert_true("crawler_voice", str(result.get("answer") or "").startswith("我是 CrawlerAgent。"))
    assert_true("no_self_handoff_voice", "转交给 CrawlerAgent" not in str(result.get("answer") or ""))
    assert_true("clean_collection_target", "CrawlerAgent 应" not in calls[0]["question"] and "用户原始目标" not in calls[0]["question"])
    summary = calls[0]["payload"].get("session_summary") or {}
    assert_true("planning_instruction_carried", "mcagent_context" in str(summary.get("planning_instruction") or ""))


def test_crawler_job_can_execute_mcagent_context_tool() -> None:
    tmp = tempfile.TemporaryDirectory()
    original_retriever = web_server.Retriever
    original_generate = web_server._generate_grounded_answer

    class FakeRetriever:
        def __init__(self, config: AppConfig):  # noqa: ARG002
            pass

        def search(self, query: str, top_k: int, plan: Any | None = None, session_summary: dict[str, Any] | None = None):  # noqa: ARG002
            return [
                SearchResult(
                    rank=1,
                    score=9.5,
                    chunk_id=1,
                    document_id=1,
                    chunk_index=0,
                    title="乌托邦探险之旅本地资料",
                    source_path=str(Path(tmp.name) / "utopia.md"),
                    url="https://example.test/utopia",
                    text="乌托邦探险之旅已有基础介绍，但缺少完整模组列表、任务线和 Boss 攻略。",
                    metadata={},
                )
            ]

    web_server.Retriever = FakeRetriever  # type: ignore[assignment]
    web_server._generate_grounded_answer = (  # type: ignore[assignment]
        lambda *args, **kwargs: ("MCagent 回复 CrawlerAgent：本地已有基础介绍，缺少完整模组列表、任务线和 Boss 攻略。", "context")
    )
    try:
        result = web_server._run_mcagent_context_tool(
            make_temp_config(Path(tmp.name)),
            {"query": "乌托邦整合包", "question": "问下MCAgent乌托邦整合包还缺哪些东西"},
            {"delivery_target": "MCagent/RAG"},
            {"gaps": ["完整模组列表", "任务线"]},
        )
    finally:
        web_server.Retriever = original_retriever  # type: ignore[assignment]
        web_server._generate_grounded_answer = original_generate  # type: ignore[assignment]
        tmp.cleanup()

    try:
        assert_equal("source", result["source"], "mcagent_context")
        assert_equal("returncode", result["returncode"], 0)
        assert_true("mcagent_answer", "CrawlerAgent" in str(result.get("mcagent_answer") or ""))
        assert_true("has_gap_summary", "完整模组列表" in str(result.get("mcagent_gap_summary") or ""))
        export_dir = Path(str(result.get("export_dir") or ""))
        manifest = json.loads((export_dir / "manifest.json").read_text(encoding="utf-8"))
        assert_equal("manifest_source", manifest["source"], "mcagent_context")
        assert_equal("inter_agent_from", manifest["inter_agent"]["from_agent"], "CrawlerAgent")
        assert_equal("inter_agent_to", manifest["inter_agent"]["to_agent"], "MCagent")
        assert_true("reply_persisted", "CrawlerAgent" in manifest["inter_agent"]["reply"])
        assert_true("manifest_records", len(manifest.get("records") or []) == 1)
    finally:
        if result.get("export_dir"):
            shutil.rmtree(str(result["export_dir"]), ignore_errors=True)


def test_mcagent_context_tool_timeout_returns_objective_blocker() -> None:
    original_inner = web_server._run_mcagent_context_tool_inner
    original_timeout = web_server.DEFAULT_MCAGENT_CONTEXT_TIMEOUT_SECONDS

    def slow_inner(*args, **kwargs):  # noqa: ANN002, ANN003
        time.sleep(0.2)
        return {"source": "mcagent_context", "returncode": 0}

    web_server._run_mcagent_context_tool_inner = slow_inner  # type: ignore[assignment]
    web_server.DEFAULT_MCAGENT_CONTEXT_TIMEOUT_SECONDS = 0.05  # type: ignore[assignment]
    try:
        result = web_server._run_mcagent_context_tool(
            make_temp_config(Path(tempfile.gettempdir())),
            {"query": "Utopian Journey"},
            {"delivery_target": "MCagent/RAG"},
            {},
        )
    finally:
        web_server._run_mcagent_context_tool_inner = original_inner  # type: ignore[assignment]
        web_server.DEFAULT_MCAGENT_CONTEXT_TIMEOUT_SECONDS = original_timeout  # type: ignore[assignment]
    assert_equal("source", result["source"], "mcagent_context")
    assert_equal("returncode", result["returncode"], 124)
    assert_equal("timed_out", result["timed_out"], True)
    assert_true("continue_download_route", "public archive/download discovery" in result["output"])


def test_mcagent_context_filters_off_topic_local_evidence() -> None:
    off_topic = SearchResult(
        rank=1,
        score=9.5,
        chunk_id=1,
        document_id=1,
        chunk_index=0,
        title="\u843d\u5e55\u66f2\uff08Closing Song\uff09\u6574\u5408\u5305\u8d44\u6599\u6c47\u603b",
        source_path="D:/magic/MC_Agent/data/crawler_exports/manual_research/closing_song.md",
        url="https://example.test/closing-song",
        text="\u8fd9\u91cc\u662f\u843d\u5e55\u66f2\u7684 Boss \u548c\u65b0\u624b\u8def\u7ebf\u8d44\u6599\u3002",
        metadata={},
    )
    on_topic = SearchResult(
        rank=2,
        score=8.0,
        chunk_id=2,
        document_id=2,
        chunk_index=0,
        title="\u4e4c\u6258\u90a6\u63a2\u9669\u4e4b\u65c5\uff08Utopian Journey\uff09\u6574\u5408\u5305",
        source_path="D:/magic/MC_Agent/data/crawler_exports/web_discovery/utopia.md",
        url="https://example.test/utopia",
        text="\u4e4c\u6258\u90a6\u63a2\u9669\u4e4b\u65c5\u6574\u5408\u5305\u7684\u7248\u672c\u548c\u73a9\u6cd5\u8d44\u6599\u3002",
        metadata={},
    )

    focus = "\u4e4c\u6258\u90a6\u63a2\u9669\u4e4b\u65c5\u6574\u5408\u5305 Utopian Journey"
    assert_equal(
        "off_topic_filtered",
        web_server._filter_mcagent_context_evidence(focus, [off_topic], {"verdict": "ok"}),
        [],
    )
    assert_equal(
        "insufficient_filtered",
        web_server._filter_mcagent_context_evidence(focus, [on_topic], {"verdict": "insufficient"}),
        [],
    )
    assert_equal(
        "on_topic_kept",
        web_server._filter_mcagent_context_evidence(focus, [off_topic, on_topic], {"verdict": "ok"}),
        [on_topic],
    )


def test_no_llm_mcagent_path_still_runs_evidence_selection() -> None:
    off_topic = SearchResult(
        rank=1,
        score=9.0,
        chunk_id=1,
        document_id=1,
        chunk_index=0,
        title="Boss直聘（2014年上线的在线招聘平台）_百度百科",
        source_path="D:/magic/MC_Agent/data/crawler_exports/jina/boss.md",
        url="https://example.test/boss",
        text="在线招聘平台资料。",
        metadata={},
    )
    on_topic = SearchResult(
        rank=2,
        score=4.0,
        chunk_id=2,
        document_id=2,
        chunk_index=0,
        title="乌托邦探险之旅 | XyeBBS",
        source_path="D:/magic/MC_Agent/data/crawler_exports/web_discovery/utopia.md",
        url="https://example.test/utopia",
        text="乌托邦探险之旅整合包，1.20.1 Fabric，包含更新日志和下载信息。",
        metadata={},
    )

    assert_equal(
        "required_term_filter",
        web_server._filter_answer_evidence_by_required_terms(
            "本地资料里乌托邦还有哪些缺口？",
            [off_topic, on_topic],
        ),
        [on_topic],
    )

    class FakeRun:
        original_question = "本地资料里乌托邦还有哪些缺口？"
        question = original_question
        agent = "mcagent_rag"
        model = ""
        temperature = 0.0
        max_tokens = 400

        def __init__(self) -> None:
            self.trace = SimpleNamespace(steps=[])

        def add_trace(self, stage, status, detail=None):
            self.trace.steps.append({"stage": stage, "status": status, "detail": detail})
            return self.trace.steps[-1]

        def response(self, payload):
            payload["trace"] = self.trace.steps
            return payload

    class FakeRouter:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def route(self, run, session_summary=None):
            return SimpleNamespace(
                tool_decision={},
                route_intent="answer",
                action_plan=[],
                rag_focus="",
                planned_workflow=False,
                planned_delegate=False,
            )

        def confirm_next_step(self, *args, **kwargs):
            return {"proceed": True, "planner": "test"}

    class FakeRag:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def prepare(self, *args, **kwargs):
            return SimpleNamespace(evidence_question="本地资料里乌托邦还有哪些缺口？", rough_k=8, final_k=6)

        def retrieve(self, *args, **kwargs):
            return SimpleNamespace(
                retrieval_plan=None,
                rough_results=[off_topic, on_topic],
                selected=[off_topic, on_topic],
            )

    original_context = web_server.build_agent_execution_context
    original_router = web_server.LlmAgentToolRouterService
    original_rag = web_server.RagRetrievalService
    original_project_keywords = web_server._supplement_project_keyword_results
    original_raw_html = web_server._supplement_raw_html_results
    original_modpack_context = web_server._ensure_modpack_mod_list_context
    try:
        web_server.build_agent_execution_context = lambda *args, **kwargs: FakeRun()  # type: ignore[assignment]
        web_server.LlmAgentToolRouterService = FakeRouter  # type: ignore[assignment]
        web_server.RagRetrievalService = FakeRag  # type: ignore[assignment]
        web_server._supplement_project_keyword_results = lambda _config, _question, selected, _limit: selected  # type: ignore[assignment]
        web_server._supplement_raw_html_results = lambda _config, _question, selected, limit=8: selected  # type: ignore[assignment]
        web_server._ensure_modpack_mod_list_context = lambda _config, _question, selected, _rough, _limit: selected  # type: ignore[assignment]
        result = web_server._chat_impl(
            SimpleNamespace(),
            {
                "agent": "mcagent_rag",
                "question": "本地资料里乌托邦还有哪些缺口？",
                "no_llm": True,
            },
        )
    finally:
        web_server.build_agent_execution_context = original_context  # type: ignore[assignment]
        web_server.LlmAgentToolRouterService = original_router  # type: ignore[assignment]
        web_server.RagRetrievalService = original_rag  # type: ignore[assignment]
        web_server._supplement_project_keyword_results = original_project_keywords  # type: ignore[assignment]
        web_server._supplement_raw_html_results = original_raw_html  # type: ignore[assignment]
        web_server._ensure_modpack_mod_list_context = original_modpack_context  # type: ignore[assignment]

    titles = [item["title"] for item in result.get("sources") or []]
    assert_true("kept_on_topic", any("乌托邦" in title for title in titles), str(titles))
    assert_true("filtered_off_topic", not any("Boss直聘" in title for title in titles), str(titles))


def test_version_install_note_extracts_modpack_requirements() -> None:
    source = SearchResult(
        rank=1,
        score=9.0,
        chunk_id=1,
        document_id=1,
        chunk_index=0,
        title="乌托邦探险之旅下载页",
        source_path="D:/magic/MC_Agent/data/crawler_exports/web_discovery/utopia_download.md",
        url="https://example.test/utopia-download",
        text=(
            "整合包下载：乌托邦探险之旅（Utopian Journey）\n"
            "最新版本：3.5.1\n"
            "历史版本：3.2-3.5.1\n"
            "Java版本需求：17-21\n"
            "安装方式：PCL启动器或HMCL启动器安装。\n"
            "内存需求：16G分配8G（关闭无关后台占用）\n"
            "32G分配10G。\n"
            "我的世界Java版本\n"
            "1.20.1\n"
            "平台\n"
            "Fabric\n"
        ),
        metadata={},
    )

    note = web_server._version_install_extraction_note("乌托邦探险之旅的版本和安装要求是什么？", [source])
    assert_true("has_pack_version", "3.5.1" in note, note)
    assert_true("has_java_requirement", "17-21" in note, note)
    assert_true("has_launcher", "PCL" in note and "HMCL" in note, note)
    assert_true("has_memory", "16G" in note and "8G" in note, note)
    assert_true("has_mc_version_loader", "1.20.1" in note and "Fabric" in note, note)

    answer = web_server._local_version_install_answer("乌托邦探险之旅的版本和安装要求是什么？", [source])
    assert_true("answer_has_pack_version", "3.5.1" in answer, answer)
    assert_true("answer_has_java_requirement", "17-21" in answer, answer)
    assert_true("answer_has_launcher", "PCL" in answer and "HMCL" in answer, answer)
    assert_true("answer_has_memory", "16G" in answer and "8G" in answer, answer)
    assert_true("answer_has_mc_version_loader", "1.20.1" in answer and "Fabric" in answer, answer)


def test_modpack_overview_surfaces_version_install_evidence() -> None:
    source = SearchResult(
        rank=1,
        score=9.0,
        chunk_id=1,
        document_id=1,
        chunk_index=0,
        title="乌托邦探险之旅 - 我的世界整合包 | BBSMC 下载",
        source_path="D:/magic/MC_Agent/data/crawler_exports/web_discovery/utopia_download.md",
        url="https://bbsmc.net/modpack/utopia-journey",
        text=(
            "乌托邦探险之旅\n"
            "基本信息\n"
            "我的世界Java版本\n"
            "1.20.1\n"
            "平台\n"
            "Fabric\n"
            "运行环境\n"
            "客户端和服务端\n"
        ),
        metadata={},
    )

    note = web_server._version_install_extraction_note("乌托邦探险之旅 Utopian Journey 是什么整合包？", [source])
    assert_true("overview_has_mc_version", "1.20.1" in note, note)
    assert_true("overview_has_loader", "Fabric" in note, note)


def test_specific_utopian_journey_filter_rejects_generic_utopian_sources() -> None:
    generic = SearchResult(
        rank=1,
        score=5.0,
        chunk_id=1,
        document_id=1,
        chunk_index=0,
        title="Utopian Armor - Advent of Ascension",
        source_path="D:/magic/MC_Agent/data/crawler_exports/mcmod/utopian_armor.md",
        url="https://www.mcmod.cn/item/489325.html",
        text="Utopian Armor is an item from Advent of Ascension.",
        metadata={},
    )
    target = SearchResult(
        rank=2,
        score=4.0,
        chunk_id=2,
        document_id=2,
        chunk_index=0,
        title="乌托邦探险之旅 - 我的世界整合包 | BBSMC 下载",
        source_path="D:/magic/MC_Agent/data/crawler_exports/web_discovery/utopia_journey.md",
        url="https://bbsmc.net/modpack/utopia-journey/",
        text="乌托邦探险之旅 Utopian Journey Java 1.20.1 Fabric.",
        metadata={},
    )
    filtered = web_server._filter_answer_evidence_by_required_terms(
        "乌托邦探险之旅这个整合包适合什么 Minecraft 版本和加载器？",
        [generic, target],
    )
    assert_equal("specific_filter", filtered, [target])


def test_specific_utopian_journey_filter_rejects_other_pack_mentions() -> None:
    other_pack = SearchResult(
        rank=1,
        score=6.0,
        chunk_id=1,
        document_id=1,
        chunk_index=0,
        title="落幕曲（Closing Song）整合包资料汇总",
        source_path="D:/magic/MC_Agent/data/crawler_exports/manual_research/closing_song.md",
        url="https://example.test/closing-song",
        text="这里顺带提到乌托邦探险之旅作为对比，但本文主体是落幕曲。",
        metadata={},
    )
    target = SearchResult(
        rank=2,
        score=5.0,
        chunk_id=2,
        document_id=2,
        chunk_index=0,
        title="[UJ]乌托邦探险之旅 (Utopian Journey) - MC百科",
        source_path="D:/magic/MC_Agent/data/crawler_exports/fetch_url/utopian_journey.md",
        url="https://www.mcmod.cn/modpack/1337.html",
        text="乌托邦探险之旅 Utopian Journey 整合包。",
        metadata={},
    )
    filtered = web_server._filter_answer_evidence_by_required_terms(
        "乌托邦探险之旅有哪些模组列表、任务线、玩法机制资料？",
        [other_pack, target],
    )
    assert_equal("strict_other_pack_filter", filtered, [target])


def test_version_install_extraction_ignores_mcmod_navigation_loaders() -> None:
    text = (
        "版本检索\n"
        "Forge 整合包\n"
        "Fabric 整合包\n"
        "1.20.1 整合包\n"
        "1.19.4 整合包\n"
        "基本信息\n"
        "我的世界Java版本\n"
        "1.20.1\n"
        "平台\n"
        "Fabric\n"
    )
    facts = web_server._extract_version_install_fact_map(text)
    labels = web_server._version_install_fact_labels()
    assert_equal("loader_only_real_platform", facts.get(labels[2]), ["Fabric"])
    assert_true("mc_version_kept", "1.20.1" in (facts.get(labels[1]) or []), str(facts))


def test_local_version_install_answer_ignores_wrong_modpack_sources() -> None:
    wrong = SearchResult(
        rank=1,
        score=5.0,
        chunk_id=1,
        document_id=1,
        chunk_index=0,
        title="落幕曲整合包资料汇总",
        source_path="D:/magic/MC_Agent/data/crawler_exports/manual_research/closing_song.md",
        url="https://example.test/closing-song",
        text="落幕曲整合包\n平台\nForge\n运行环境\n客户端 服务端",
        metadata={},
    )
    answer = web_server._local_version_install_answer(
        "乌托邦探险之旅这个整合包适合什么 Minecraft 版本和加载器？",
        [wrong],
    )
    assert_equal("no_wrong_answer", answer, "")


def test_version_install_fact_question_bypasses_llm_router() -> None:
    question = "What are the version and install requirements for Utopian Journey?"
    source = SearchResult(
        rank=1,
        score=9.0,
        chunk_id=1,
        document_id=1,
        chunk_index=0,
        title="Utopian Journey download page",
        source_path="D:/magic/MC_Agent/data/crawler_exports/web_discovery/utopia_download.md",
        url="https://example.test/utopia-download",
        text=(
            "Utopian Journey\n"
            "Latest version: 3.5.1\n"
            "Java requirement: 17-21\n"
            "Install method: PCL or HMCL launcher\n"
            "Memory requirement: 16G RAM, allocate 8G\n"
            "Minecraft Java version\n"
            "1.20.1\n"
            "Platform\n"
            "Fabric\n"
        ),
        metadata={},
    )

    class FakeRun:
        original_question = "What are the version and install requirements for Utopian Journey?"
        question = original_question
        agent = "mcagent_rag"
        model = ""
        temperature = 0.0
        max_tokens = 400

        def __init__(self) -> None:
            self.trace = SimpleNamespace(steps=[])

        def add_trace(self, stage, status, detail=None):
            self.trace.steps.append({"stage": stage, "status": status, "detail": detail})
            return self.trace.steps[-1]

        def response(self, payload):
            payload["trace"] = self.trace.steps
            return payload

    class RouterMustNotRun:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def route(self, *args, **kwargs):
            raise AssertionError("version/install local fact route should bypass LLM router")

        def confirm_next_step(self, *args, **kwargs):
            raise AssertionError("version/install local fact route should bypass LLM confirmations")

    class FakeRag:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def prepare(self, *args, **kwargs):
            return SimpleNamespace(evidence_question=question, rough_k=8, final_k=6)

        def retrieve(self, *args, **kwargs):
            assert_true("planner_disabled", kwargs.get("use_planner") is False)
            return SimpleNamespace(retrieval_plan=None, rough_results=[source], selected=[source])

    original_context = web_server.build_agent_execution_context
    original_router = web_server.LlmAgentToolRouterService
    original_rag = web_server.RagRetrievalService
    original_project_keywords = web_server._supplement_project_keyword_results
    original_raw_html = web_server._supplement_raw_html_results
    original_modpack_context = web_server._ensure_modpack_mod_list_context
    try:
        web_server.build_agent_execution_context = lambda *args, **kwargs: FakeRun()  # type: ignore[assignment]
        web_server.LlmAgentToolRouterService = RouterMustNotRun  # type: ignore[assignment]
        web_server.RagRetrievalService = FakeRag  # type: ignore[assignment]
        web_server._supplement_project_keyword_results = lambda _config, _question, selected, _limit: selected  # type: ignore[assignment]
        web_server._supplement_raw_html_results = lambda _config, _question, selected, limit=8: selected  # type: ignore[assignment]
        web_server._ensure_modpack_mod_list_context = lambda _config, _question, selected, _rough, _limit: selected  # type: ignore[assignment]
        result = web_server._chat_impl(
            SimpleNamespace(),
            {
                "agent": "mcagent_rag",
                "question": question,
            },
        )
    finally:
        web_server.build_agent_execution_context = original_context  # type: ignore[assignment]
        web_server.LlmAgentToolRouterService = original_router  # type: ignore[assignment]
        web_server.RagRetrievalService = original_rag  # type: ignore[assignment]
        web_server._supplement_project_keyword_results = original_project_keywords  # type: ignore[assignment]
        web_server._supplement_raw_html_results = original_raw_html  # type: ignore[assignment]
        web_server._ensure_modpack_mod_list_context = original_modpack_context  # type: ignore[assignment]

    answer = result.get("answer") or ""
    assert_true("answer_has_pack_version", "3.5.1" in answer, answer)
    assert_true("answer_has_mc_version", "1.20.1" in answer, answer)
    assert_true("answer_has_loader", "Fabric" in answer, answer)
    statuses = [(item.get("stage"), item.get("status")) for item in result.get("trace") or []]
    assert_true("local_fact_trace", ("answer", "local_fact_answer") in statuses, str(statuses))


def test_modpack_overview_with_version_does_not_use_narrow_fact_route() -> None:
    assert_equal(
        "overview_not_narrow_fact",
        web_server._should_use_deterministic_local_fact_rag_route(
            "mcagent_rag",
            "乌托邦探险之旅 Utopian Journey 是什么整合包？请说明 Minecraft 版本和加载器。",
            {},
        ),
        False,
    )


def test_general_answer_path_skips_local_fact_answer_for_modpack_overview() -> None:
    source = (ROOT / "mcagent" / "web_server.py").read_text(encoding="utf-8")
    assert_true(
        "overview_skips_local_fact_answer",
        'answer = "" if _is_modpack_overview_question(original_question) else _local_version_install_answer(original_question, selected)'
        in source,
    )


def test_mcagent_context_tool_uses_fast_structured_reply_instead_of_second_answer_llm() -> None:
    source = (ROOT / "mcagent" / "web_server.py").read_text(encoding="utf-8")
    start = source.index("def _run_mcagent_context_tool")
    end = source.index("\ndef _crawler_reusable_duplicate_evidence", start)
    body = source[start:end]
    assert_true("no_second_grounded_answer_call", "_generate_grounded_answer(" not in body)
    assert_true("fast_context_trace", "structured_fast_context" in body)


def test_crawler_topic_match_decision_comes_from_crawler_llm() -> None:
    with tempfile.TemporaryDirectory(prefix="mcagent-topic-review-") as tmp:
        export_dir = Path(tmp)
        page = export_dir / "playwright_Modrinth.md"
        page.write_text("# Modrinth\n\nProject not found. You may have mistyped the project's URL.", encoding="utf-8")
        (export_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "records": [
                        {
                            "title": "Modrinth",
                            "url": "https://modrinth.com/project/utopia-exploration-modpack",
                            "path": str(page),
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        original = web_server._crawler_llm_record_relevance
        try:
            web_server._crawler_llm_record_relevance = lambda *args, **kwargs: {  # type: ignore[assignment]
                "matched": False,
                "reason": "not_found",
                "matched_indexes": [],
                "rejected_indexes": [0],
                "cleanup_action": "retry_other_source",
                "next_action": "Find another source.",
                "notes": "Wrong Modrinth URL.",
                "judge": "Crawler LLM",
            }
            result = web_server._crawler_topic_match(str(export_dir), "Utopian Journey", "Utopian Journey Modrinth", {})
        finally:
            web_server._crawler_llm_record_relevance = original  # type: ignore[assignment]
        assert_equal("matched", result["matched"], False)
        assert_equal("reason", result["reason"], "not_found")
        assert_equal("cleanup_action", result["cleanup_action"], "retry_other_source")
        assert_equal("rejected_title", result["rejected_examples"][0]["title"], "Modrinth")


def test_crawler_summary_uses_only_llm_matched_record_indexes() -> None:
    with tempfile.TemporaryDirectory(prefix="mcagent-accepted-summary-") as tmp:
        export_dir = Path(tmp)
        good = export_dir / "good.md"
        bad = export_dir / "bad.md"
        good.write_text("# 乌托邦探险之旅\n\nJava 1.20.1 Fabric.", encoding="utf-8")
        bad.write_text("# BFF 逆转未来\n\nUnrelated modpack.", encoding="utf-8")
        (export_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "records": [
                        {"title": "乌托邦探险之旅", "url": "https://bbsmc.net/modpack/utopia-journey/", "path": str(good), "chars": 32},
                        {"title": "BFF 逆转未来", "url": "https://www.mcmod.cn/modpack/1340.html", "path": str(bad), "chars": 26},
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        result = {
            "source": "web_discovery",
            "query": "乌托邦探险之旅",
            "returncode": 0,
            "export_dir": str(export_dir),
            "manifest_stats": web_server._crawler_manifest_stats(str(export_dir)),
            "topic_validation": {
                "matched": True,
                "reason": "direct",
                "matched_indexes": [0],
                "rejected_indexes": [1],
            },
        }
        summary = web_server._crawler_result_summary([result], {"topic": "乌托邦探险之旅"})
        titles = [item.get("title") for item in summary["useful_records"]]
        assert_equal("accepted_titles", titles, ["乌托邦探险之旅"])
        roots = web_server._crawler_accepted_ingest_roots(result)
        assert_equal("one_root", len(roots), 1)
        accepted_root = Path(roots[0])
        assert_true("accepted_good", (accepted_root / "good.md").exists())
        assert_true("rejected_bad", not (accepted_root / "bad.md").exists())


def test_duplicate_reuse_requires_crawler_llm_acceptance() -> None:
    with tempfile.TemporaryDirectory(prefix="mcagent-dup-review-") as tmp:
        root = Path(tmp)
        previous = root / "previous.md"
        previous.write_text("# Modrinth\n\nProject not found. You may have mistyped the project's URL.", encoding="utf-8")
        export_dir = root / "export"
        export_dir.mkdir()
        (export_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "records": [],
                    "skipped": [
                        {
                            "title": "Modrinth",
                            "url": "https://modrinth.com/project/utopia-exploration-modpack",
                            "previous_path": str(previous),
                            "reason": "url_or_content_duplicate",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        original = web_server._crawler_llm_record_relevance
        try:
            web_server._crawler_llm_record_relevance = lambda *args, **kwargs: {  # type: ignore[assignment]
                "matched": False,
                "reason": "not_found",
                "matched_indexes": [],
                "rejected_indexes": [0],
                "cleanup_action": "retry_other_source",
                "next_action": "Do not reuse this duplicate 404 page.",
                "judge": "Crawler LLM",
            }
            result = web_server._crawler_reusable_duplicate_evidence(str(export_dir), "Utopian Journey", "Utopian Journey Modrinth", {})
        finally:
            web_server._crawler_llm_record_relevance = original  # type: ignore[assignment]
        assert_equal("matched", result["matched"], False)
        assert_equal("reason", result["reason"], "not_found")
        assert_equal("cleanup_action", result["cleanup_action"], "retry_other_source")
        assert_equal("records", result["records"], [])


def test_modpack_internal_missing_archive_reports_objective_blocker() -> None:
    command = web_server._round_command("modpack_internal", {"query": "definitely-no-such-pack-archive"})
    completed = subprocess.run(command, cwd=str(ROOT), text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert_equal("returncode", completed.returncode, 2)
    data = json.loads(completed.stdout)
    assert_equal("provider", data["provider"], "modpack_internal")
    assert_equal("archive_found", data["archive_found"], False)
    assert_true("failure_reason", "No matching local modpack archive" in data["failure_reason"])
    stats = web_server._inline_failure_manifest_stats({"returncode": completed.returncode, "output": completed.stdout})
    assert_equal("stats_errors", stats["errors"], 1)
    assert_true("stats_next_action", "modpack_download" in stats["next_action"])


def test_modpack_download_accepts_direct_archive_url_as_candidate() -> None:
    from unittest.mock import patch  # noqa: PLC0415

    from scripts.fetch_modpack_archive_seed import archive_link_candidates  # noqa: PLC0415

    with patch("scripts.fetch_modpack_archive_seed.urllib.request.urlopen", side_effect=RuntimeError("no network in unit test")):
        candidates, pages, errors = archive_link_candidates("https://example.com/packs/demo.mrpack", user_agent="unit-test", limit=3)
    assert_equal("candidate_count", len(candidates), 1)
    assert_equal("candidate_source", candidates[0]["source"], "direct_url")
    assert_equal("candidate_url", candidates[0]["url"], "https://example.com/packs/demo.mrpack")
    assert_equal("pages", pages, [])
    assert_equal("errors", errors, [])


def test_modpack_download_reports_bbsmc_cloud_drive_blocker() -> None:
    from unittest.mock import patch  # noqa: PLC0415

    from scripts import fetch_modpack_archive_seed as seed  # noqa: PLC0415

    def fake_request_text(url: str, user_agent: str, timeout: int = 30):  # noqa: ARG001
        if url.startswith("https://api.bbsmc.net/v2/search"):
            return (
                json.dumps(
                    {
                        "hits": [
                            {
                                "project_id": "1p2TFl6X",
                                "project_type": "modpack",
                                "slug": "utopia-journey",
                                "title": "乌托邦探险之旅",
                                "description": "乌托邦探险之旅",
                                "versions": ["1.20.1"],
                                "downloads": 1993041,
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                "application/json",
                200,
            )
        if url == "https://api.bbsmc.net/v2/project/utopia-journey":
            return (
                json.dumps(
                    {
                        "id": "1p2TFl6X",
                        "slug": "utopia-journey",
                        "project_type": "modpack",
                        "title": "乌托邦探险之旅",
                        "description": "乌托邦探险之旅",
                        "downloads": 1993041,
                        "game_versions": ["1.20.1"],
                        "loaders": ["fabric"],
                    },
                    ensure_ascii=False,
                ),
                "application/json",
                200,
            )
        if url == "https://api.bbsmc.net/v2/project/utopia-journey/version":
            return (
                json.dumps(
                    [
                        {
                            "name": "乌托邦探险之旅3.5.2",
                            "version_number": "3.5.2",
                            "downloads": 195615,
                            "game_versions": ["1.20.1"],
                            "loaders": ["fabric"],
                            "disk_only": True,
                            "files": [
                                {
                                    "url": "https://pan.quark.cn/s/76148f08445c",
                                    "filename": "",
                                    "primary": False,
                                    "size": 0,
                                }
                            ],
                            "disk_urls": [
                                {"platform": "quark", "url": "https://pan.quark.cn/s/76148f08445c"},
                                {"platform": "xunlei", "url": "https://pan.xunlei.com/s/demo?pwd=32zd"},
                            ],
                        }
                    ],
                    ensure_ascii=False,
                ),
                "application/json",
                200,
            )
        raise RuntimeError(f"unexpected url: {url}")

    with patch.object(seed, "request_text", side_effect=fake_request_text):
        candidates, pages, blockers, errors = seed.bbsmc_archive_candidates("Utopian Journey modpack .mrpack .zip", user_agent="unit-test", limit=5)
    assert_equal("candidates", candidates, [])
    assert_equal("errors", errors, [])
    assert_true("has_pages", len(pages) >= 2)
    assert_equal("blocker_count", len(blockers), 3)
    assert_true("blocker_reason", all("direct" in item["reason"] or "cloud" in item["reason"].lower() for item in blockers))
    assert_true("bbsmc_project_url", any(page.get("url") == "https://bbsmc.net/modpack/utopia-journey" for page in pages))


def test_job_to_dict_hides_unqualified_modpack_internal_from_history_view() -> None:
    job = web_server.Job(
        id="test-job",
        kind="crawler",
        title="Crawler",
        status="succeeded",
        created_at=1.0,
        started_at=1.0,
        ended_at=2.0,
        summary="done",
        result={
            "plan": {"topic": "乌托邦探险之旅"},
            "planned_tasks": [
                {"source": "web_discovery", "query": "乌托邦探险之旅 攻略"},
                {"source": "modpack_internal", "query": "Utopian Journey"},
            ],
            "tasks": [],
        },
    )
    payload = web_server._job_to_dict(job)
    planned_sources = [task["source"] for task in payload["result"]["planned_tasks"]]
    assert_equal("planned_sources", planned_sources, ["web_discovery"])
    assert_equal("blocked_count", len(payload["result"]["blocked_planned_tasks"]), 1)
    assert_equal("readable_total", payload["readable"]["total_tasks"], 1)
    assert_equal("readable_blocked", len(payload["readable"]["blocked_planned_tasks"]), 1)


if __name__ == "__main__":
    test_direct_crawler_no_save_url_uses_temporary_extract_boundary()
    test_grounded_answer_does_not_fallback_to_ollama_after_profile_error()
    test_direct_user_handoff_brief_rejects_wrong_mcagent_identity()
    test_direct_crawler_delegate_choice_is_corrected_to_temporary_extract()
    test_direct_crawler_mcagent_gap_request_forces_planned_workflow()
    test_direct_crawler_delegate_gap_request_is_rewritten_to_context_workflow()
    test_direct_crawler_router_error_gap_request_recovers_to_context_workflow()
    test_mcagent_context_focus_expands_minecraft_utopia_aliases()
    test_successful_mcagent_context_prunes_duplicate_pending_context_tasks()
    test_successful_mcagent_context_filters_new_duplicate_context_tasks()
    test_runtime_status_request_bypasses_llm_router()
    test_mcagent_gap_delegation_overrides_human_delivery_to_rag()
    test_explicit_mcagent_to_crawler_handoff_starts_job_before_router()
    test_mcagent_explicit_crawler_request_forces_planned_delegate()
    test_direct_crawler_mcagent_gap_request_delegates_when_local_empty()
    test_crawler_mcagent_context_with_collection_continues_to_delegate()
    test_direct_crawler_delegate_choice_runs_as_crawler_context_workflow()
    test_crawler_job_can_execute_mcagent_context_tool()
    test_mcagent_context_tool_timeout_returns_objective_blocker()
    test_mcagent_context_filters_off_topic_local_evidence()
    test_specific_utopian_journey_filter_rejects_generic_utopian_sources()
    test_specific_utopian_journey_filter_rejects_other_pack_mentions()
    test_version_install_extraction_ignores_mcmod_navigation_loaders()
    test_local_version_install_answer_ignores_wrong_modpack_sources()
    test_no_llm_mcagent_path_still_runs_evidence_selection()
    test_version_install_note_extracts_modpack_requirements()
    test_modpack_overview_surfaces_version_install_evidence()
    test_version_install_fact_question_bypasses_llm_router()
    test_modpack_overview_with_version_does_not_use_narrow_fact_route()
    test_general_answer_path_skips_local_fact_answer_for_modpack_overview()
    test_mcagent_context_tool_uses_fast_structured_reply_instead_of_second_answer_llm()
    test_crawler_topic_match_decision_comes_from_crawler_llm()
    test_crawler_summary_uses_only_llm_matched_record_indexes()
    test_duplicate_reuse_requires_crawler_llm_acceptance()
    test_modpack_internal_missing_archive_reports_objective_blocker()
    test_modpack_download_accepts_direct_archive_url_as_candidate()
    test_modpack_download_reports_bbsmc_cloud_drive_blocker()
    test_job_to_dict_hides_unqualified_modpack_internal_from_history_view()
    print("web_server_side_effect_guard_scenarios passed")
