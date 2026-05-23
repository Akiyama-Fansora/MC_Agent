from __future__ import annotations

from pathlib import Path
import json
import shutil
import sys
import tempfile
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
    web_server.RagRetrievalService.retrieve = (  # type: ignore[assignment]
        lambda self, *args, **kwargs: SimpleNamespace(retrieval_plan=None, rough_results=[], selected=[])
    )
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
    assert_true("delegated_after_context", bool(calls))
    assert_equal("delivery_target", result.get("delegation", {}).get("delivery_target"), "MCagent/RAG")


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


def test_crawler_job_can_execute_mcagent_context_tool() -> None:
    tmp = tempfile.TemporaryDirectory()
    original_retriever = web_server.Retriever

    class FakeRetriever:
        def __init__(self, config: AppConfig):  # noqa: ARG002
            pass

        def search(self, query: str, top_k: int, session_summary: dict[str, Any] | None = None):  # noqa: ARG002
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
    try:
        result = web_server._run_mcagent_context_tool(
            make_temp_config(Path(tmp.name)),
            {"query": "乌托邦整合包", "question": "问下MCAgent乌托邦整合包还缺哪些东西"},
            {"delivery_target": "MCagent/RAG"},
            {"gaps": ["完整模组列表", "任务线"]},
        )
    finally:
        web_server.Retriever = original_retriever  # type: ignore[assignment]
        tmp.cleanup()

    try:
        assert_equal("source", result["source"], "mcagent_context")
        assert_equal("returncode", result["returncode"], 0)
        assert_true("has_gap_summary", "完整模组列表" in str(result.get("mcagent_gap_summary") or ""))
        export_dir = Path(str(result.get("export_dir") or ""))
        manifest = json.loads((export_dir / "manifest.json").read_text(encoding="utf-8"))
        assert_equal("manifest_source", manifest["source"], "mcagent_context")
        assert_true("manifest_records", len(manifest.get("records") or []) == 1)
    finally:
        if result.get("export_dir"):
            shutil.rmtree(str(result["export_dir"]), ignore_errors=True)


if __name__ == "__main__":
    test_direct_crawler_no_save_url_uses_temporary_extract_boundary()
    test_direct_user_handoff_brief_rejects_wrong_mcagent_identity()
    test_direct_crawler_delegate_choice_is_corrected_to_temporary_extract()
    test_direct_crawler_mcagent_gap_request_forces_planned_workflow()
    test_direct_crawler_delegate_gap_request_is_rewritten_to_context_workflow()
    test_direct_crawler_router_error_gap_request_recovers_to_context_workflow()
    test_mcagent_context_focus_expands_minecraft_utopia_aliases()
    test_direct_crawler_mcagent_gap_request_delegates_when_local_empty()
    test_crawler_mcagent_context_with_collection_continues_to_delegate()
    test_direct_crawler_delegate_choice_runs_as_crawler_context_workflow()
    test_crawler_job_can_execute_mcagent_context_tool()
    print("web_server_side_effect_guard_scenarios passed")
