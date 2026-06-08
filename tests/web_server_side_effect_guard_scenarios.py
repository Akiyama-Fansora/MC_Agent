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
import mcagent.retriever as retriever  # noqa: E402
from mcagent.config import AppConfig, ChunkingConfig, EmbeddingConfig, OllamaConfig, PathsConfig, RetrievalConfig  # noqa: E402
from mcagent.schema import RawDocument, SearchResult, TextChunk  # noqa: E402
from mcagent.storage import connect, fetch_chunks_by_ids, init_db, replace_document  # noqa: E402


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


def test_search_local_files_command_has_no_project_root_default() -> None:
    command = web_server._round_command(
        "search_local_files",
        {"source": "search_local_files", "query": "Farmer's Delight", "output_dir": r"D:\tmp\crawler-output"},
    )
    joined = "\n".join(command)
    assert_true("does_not_default_project_root", str(ROOT) not in joined, joined)
    assert_true("empty_path_exposes_bad_payload", command[command.index("--path") + 1] == "", command)


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


def test_auto_max_tokens_uses_bounded_adaptive_limit() -> None:
    auto_value = web_server._answer_max_tokens({"max_tokens": "auto"}, "Farmer's Delight 农夫乐事 是什么？")
    unlimited_value = web_server._answer_max_tokens({"max_tokens": "unlimited"}, "Farmer's Delight 农夫乐事 是什么？")
    explicit_value = web_server._answer_max_tokens({"max_tokens": 9000}, "Farmer's Delight 农夫乐事 是什么？")
    assert_true("auto_is_bounded", isinstance(auto_value, int) and 1200 <= auto_value <= web_server.ANSWER_MAX_TOKENS_CAP, str(auto_value))
    assert_equal("unlimited_still_allowed_when_explicit", unlimited_value, None)
    assert_equal("explicit_is_capped", explicit_value, web_server.ANSWER_MAX_TOKENS_CAP)


def test_version_fact_answer_requires_subject_in_title_or_source() -> None:
    farmers_delight = "\u519c\u592b\u4e50\u4e8b"
    question = farmers_delight + "\u662f\u4ec0\u4e48\uff1f\u8bf7\u8bf4\u660e\u5b83\u652f\u6301\u7684\u7248\u672c/\u52a0\u8f7d\u5668\uff0c\u4ee5\u53ca\u9879\u76ee\u9875\u6216\u4e0b\u8f7d\u9875\u6709\u54ea\u4e9b\u3002"
    wanted = SearchResult(
        rank=1,
        score=9.0,
        chunk_id=1,
        document_id=1,
        chunk_index=0,
        title=f"[FD]{farmers_delight} (Farmer's Delight)",
        source_path=r"D:\case\accepted_by_crawler\Farmer-s-Delight.md",
        url="https://www.mcmod.cn/class/2820.html",
        text="\u8fd0\u884c\u73af\u5883: \u5ba2\u6237\u7aef\u9700\u88c5, \u670d\u52a1\u7aef\u9700\u88c5\nForge\nFabric \u7248\u672c\u76f8\u5173\n",
    )
    pack_mention = SearchResult(
        rank=2,
        score=8.0,
        chunk_id=2,
        document_id=2,
        chunk_index=0,
        title="\u4e4c\u6258\u90a6\u63a2\u9669\u4e4b\u65c5 pack internal inventory",
        source_path=r"D:\case\utopia\pack_inventory.md",
        url=None,
        text=f"{farmers_delight}\uff1a\u91cd\u7ec7 FarmersDelight-1.20.1-2.4.1+refabricated.jar\nFabric\n",
    )
    other_mod = SearchResult(
        rank=3,
        score=7.0,
        chunk_id=3,
        document_id=3,
        chunk_index=0,
        title="I18nUpdateMod",
        source_path=r"D:\case\mod_i18nupdatemod.md",
        url="https://modrinth.com/mod/i18nupdatemod",
        text="Mod\u52a0\u8f7d\u5668\uff1aMinecraftForge\u3001NeoForge\u3001Fabric\u3001Quilt \u90fd\u652f\u6301\n",
    )
    answer = web_server._local_version_install_answer(question, [wanted, pack_mention, other_mod])
    assert_true("uses_wanted_source", "[S1]" in answer, answer)
    assert_true("rejects_pack_mention", "[S2]" not in answer, answer)
    assert_true("rejects_other_mod", "[S3]" not in answer, answer)


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


def test_direct_crawler_no_save_without_url_discovers_then_temporary_extracts() -> None:
    tmp = tempfile.TemporaryDirectory()
    fake_client = SequencedClient(
        [
            '{"tool":"delegate_crawler","reason":"mistaken persistent route","collection_target":"Playwright Python Trace Viewer tracing official docs","delivery_target":"human"}',
            '{"proceed":true,"tool":"delegate_crawler","reason":"confirm mistaken route"}',
            '{"url":"https://playwright.dev/python/docs/trace-viewer","reason":"official Playwright Python Trace Viewer docs"}',
            "Trace Viewer summary.",
        ]
    )
    original_selector = web_server._selected_llm_client
    original_search = web_server.CrawlerTemporaryExtractService.search_candidates
    original_fetch_text = web_server.CrawlerTemporaryExtractService.fetch_text
    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake_client, "fake")  # type: ignore[assignment]
    web_server.CrawlerTemporaryExtractService.search_candidates = (  # type: ignore[assignment]
        lambda self, query, *, limit=8, timeout=12: [
            {
                "rank": 1,
                "title": "Trace viewer | Playwright Python",
                "url": "https://playwright.dev/python/docs/trace-viewer",
                "snippet": "Record traces and open them with the Playwright trace viewer.",
            }
        ]
    )
    web_server.CrawlerTemporaryExtractService.fetch_text = (  # type: ignore[assignment]
        lambda self, url, *, fetch=None: ("Trace viewer", "Trace viewer body text. " * 20, "text/html", 200)
    )
    try:
        result = web_server._chat_impl(
            make_temp_config(Path(tmp.name)),
            {
                "agent": "crawler_agent",
                "question": "Read the official Playwright Python Trace Viewer tracing docs and summarize them in chat only. Do not save locally and do not ingest.",
                "session_id": "direct-crawler-no-url-temp-test",
                "model": "fake-model",
            },
        )
    finally:
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        web_server.CrawlerTemporaryExtractService.search_candidates = original_search  # type: ignore[assignment]
        web_server.CrawlerTemporaryExtractService.fetch_text = original_fetch_text  # type: ignore[assignment]
        tmp.cleanup()

    statuses = [(step["stage"], step["status"]) for step in result.get("trace", [])]
    assert_true("boundary_trace", ("decide", "side_effect_boundary_corrected") in statuses, str(statuses))
    assert_true("discovery_trace", ("extract", "temporary_url_discovering") in statuses, str(statuses))
    assert_true("selected_trace", ("extract", "temporary_url_selected") in statuses, str(statuses))
    assert_true("temporary_result", result.get("temporary_extract", {}).get("saved_to_local") is False)
    assert_equal("temporary_url", result.get("temporary_extract", {}).get("url"), "https://playwright.dev/python/docs/trace-viewer")
    assert_true("no_background_job", "job" not in result)


def test_structured_save_request_is_not_corrected_to_temporary_extract() -> None:
    tmp = tempfile.TemporaryDirectory()
    output_dir = Path(tmp.name) / "items"
    question = (
        "Use Crawler to open https://webscraper.io/test-sites/e-commerce/static/computers/laptops, "
        "extract the first 5 products with name, price, and link, "
        f"then save xlsx, csv, and json outputs to {output_dir}."
    )
    fake_client = SequencedClient(
        [
            json.dumps(
                {
                    "tool": "delegate_crawler",
                    "reason": "persistent structured browser collection is required",
                    "collection_target": question,
                    "delivery_target": "human",
                    "action_plan": [{"step": 1, "tool": "delegate_crawler", "goal": "collect rows and save files"}],
                },
                ensure_ascii=False,
            ),
            '{"proceed":true,"tool":"delegate_crawler","reason":"confirmed persistent save"}',
            '{"handoff_brief":"User directly asked CrawlerAgent to collect structured product rows and save xlsx/csv/json files.","reason":"handoff"}',
        ]
    )
    original_selector = web_server._selected_llm_client
    original_delegate = web_server._start_crawler_job_from_crawler_tool
    calls: list[dict[str, Any]] = []

    def fake_delegate(config: AppConfig, payload: dict[str, Any], question: str, plan: dict[str, Any] | None = None):  # noqa: ARG001
        calls.append({"payload": payload, "question": question, "plan": plan})
        job = web_server.Job(id="structured-save-job", kind="crawler", title=question, status="queued", summary="queued")
        job.result = {"plan": {"topic": question, "delivery_target": payload.get("delivery_target")}}
        return job, True

    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake_client, "fake")  # type: ignore[assignment]
    web_server._start_crawler_job_from_crawler_tool = fake_delegate  # type: ignore[assignment]
    try:
        result = web_server._chat_impl(
            make_temp_config(Path(tmp.name)),
            {
                "agent": "crawler_agent",
                "question": question,
                "session_id": "structured-save-side-effect-test",
                "model": "fake-model",
            },
        )
    finally:
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        web_server._start_crawler_job_from_crawler_tool = original_delegate  # type: ignore[assignment]
        tmp.cleanup()

    statuses = [(step["stage"], step["status"]) for step in result.get("trace", [])]
    assert_true("no_temporary_boundary_trace", ("decide", "side_effect_boundary_corrected") not in statuses, str(statuses))
    assert_true("delegated", bool(calls), str(result))
    assert_true("save_target_preserved", str(output_dir) in calls[0]["question"], calls[0]["question"])
    assert_true("job_started", bool((result.get("job") or {}).get("id")), str(result))


def test_user_requested_output_dir_gets_final_delivery_files() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        export_dir = root / "export"
        export_dir.mkdir()
        markdown = export_dir / "page.md"
        markdown.write_text("# Installing Packages\n\nUse pip install.", encoding="utf-8")
        (export_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "records": [
                        {
                            "title": "Installing Packages - Python Packaging User Guide",
                            "url": "https://packaging.python.org/en/latest/tutorials/installing-packages/",
                            "path": str(markdown),
                            "format": "md",
                            "bytes": markdown.stat().st_size,
                        }
                    ],
                    "skipped": [],
                    "errors": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        output_dir = root / "delivery"
        payload = {
            "question": f"Collect packaging docs and save Markdown and JSON to {output_dir}",
            "output_dir": str(output_dir),
        }
        result = web_server._export_crawler_user_delivery(
            payload=payload,
            plan={"topic": "Python Packaging User Guide"},
            task_results=[{"source": "web_discovery", "query": "pip install", "export_dir": str(export_dir)}],
            collection_summary={"success_count": 1},
        )
        assert_equal("status", result.get("status"), "ok")
        assert_true("md_exists", (output_dir / "crawler_result.md").exists())
        assert_true("json_exists", (output_dir / "crawler_result.json").exists())
        assert_true("md_mentions_url", "packaging.python.org" in (output_dir / "crawler_result.md").read_text(encoding="utf-8"))


def test_mcagent_context_focus_expands_minecraft_utopia_aliases() -> None:
    focus = web_server._mcagent_context_focus("问下MCAgent乌托邦整合包还缺哪些东西，你去网上找补给他")
    assert_true("focus_keeps_user_topic", "乌托邦" in focus)
    assert_true("focus_adds_full_pack_name", "乌托邦探险之旅" in focus)
    assert_true("focus_adds_english_alias", "Utopian Journey" in focus)
    assert_true("focus_drops_meta_words", "问" not in focus and "MCAgent" not in focus and "网上" not in focus and "补给他" not in focus)
    assert_true("focus_drops_leftover_suffix", not focus.startswith("下"))


def test_mcagent_context_focus_keeps_gap_dimension_without_meta_instruction() -> None:
    focus = web_server._mcagent_context_focus("先问 MCagent 本地关于乌托邦探险之旅还缺哪些玩法路线资料，然后你去网上补充给 MCagent/RAG。")
    assert_true("focus_keeps_entity", "乌托邦探险之旅" in focus)
    assert_true("focus_keeps_dimension", "玩法路线" in focus)
    assert_true("focus_drops_handoff_meta", "先问" not in focus and "本地关于" not in focus and "然后" not in focus and "MCagent" not in focus)


def test_mcagent_context_focus_compacts_inventory_noise_to_entity_and_dimensions() -> None:
    noisy = (
        "请 本地 后 的缺失列表，乌托邦整合包相关的Minecraft资料 "
        "本地已有整合包311篇 模组资料98篇 缺少模组列表 任务线 Boss 玩法指南"
    )
    focus = web_server._mcagent_context_focus(noisy, noisy)
    assert_true("keeps_entity_alias", "乌托邦探险之旅" in focus and "Utopian Journey" in focus)
    assert_true("keeps_dimensions", all(term in focus for term in ("资料缺口", "模组列表", "任务线", "Boss", "玩法指南")))
    assert_true("bounded_focus", len(focus) <= 220, focus)
    assert_true("drops_inventory_counts", "311" not in focus and "98" not in focus and "本地已有" not in focus)
    assert_true("drops_leftover_words", "请 本地 后" not in focus and not focus.startswith(("请", "后", "下")))


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


def test_reflection_local_source_request_prevents_context_skip() -> None:
    task_results = [
        {
            "source": "mcagent_context",
            "returncode": 0,
            "manifest_stats": {"records": 2},
            "mcagent_source_paths": ["D:\\magic\\MC_Agent\\data\\crawler_exports\\manual_research\\utopia_quests.md"],
        }
    ]
    reflection = {"action": "replan", "reason": "Need to read local source paths before broad web retry."}
    assert_true(
        "local_source_materialization_requested",
        web_server._reflection_requests_local_source_materialization(reflection, task_results),
    )


def test_reflection_local_evidence_wording_requests_materialization() -> None:
    task_results = [
        {
            "source": "mcagent_context",
            "returncode": 0,
            "manifest_stats": {"records": 2},
            "mcagent_source_paths": ["D:\\magic\\MC_Agent\\data\\crawler_exports\\manual_research\\utopia_route.md"],
        }
    ]
    reflection = {
        "action": "replan",
        "reason": "MCagent reply contains local files with pack internals and MC百科 page; read them first before broader web search.",
    }
    assert_true(
        "local_files_materialization_requested",
        web_server._reflection_requests_local_source_materialization(reflection, task_results),
    )


def test_materializes_local_source_path_tasks_after_mcagent_context_reflection() -> None:
    task_results = [
        {
            "source": "mcagent_context",
            "returncode": 0,
            "manifest_stats": {"records": 2},
            "mcagent_source_paths": [
                "D:\\magic\\MC_Agent\\data\\crawler_exports\\manual_research\\utopia_route.md",
                "D:\\magic\\MC_Agent\\data\\crawler_exports\\fetch_url\\utopia_page.md",
            ],
        }
    ]
    reflection = {
        "action": "replan",
        "reason": "Need to inspect local source paths for version, download/archive, mod list, and progression.",
    }
    tasks = web_server._materialize_local_source_path_tasks_from_mcagent_context(
        reflection,
        task_results,
        existing_tasks=[],
        max_new_tasks=2,
    )
    assert_equal("task_count", len(tasks), 2)
    assert_true("uses_local_read_tool", all(task["source"] == "read_local_file" for task in tasks))
    assert_true("preserves_objective_path", tasks[0]["path"].endswith("utopia_route.md"))
    assert_true("query_has_dimensions", "version" in tasks[0]["query"] and "download" in tasks[0]["query"])


def test_runtime_status_request_runs_after_agent_selects_status_tool() -> None:
    tmp = tempfile.TemporaryDirectory()
    fake_client = SequencedClient(
        [
            json.dumps({"tool": "status", "reason": "inspect runtime state"}, ensure_ascii=False),
            json.dumps({"proceed": True, "tool": "status", "reason": "confirmed"}, ensure_ascii=False),
        ]
    )
    original_selector = web_server._selected_llm_client
    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake_client, "fake")  # type: ignore[assignment]
    try:
        result = web_server._chat_impl(
            make_temp_config(Path(tmp.name)),
            {"agent": "mcagent_rag", "question": "状态", "session_id": "runtime-status-agent-selected"},
        )
    finally:
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        tmp.cleanup()

    statuses = [(step["stage"], step["status"]) for step in result.get("trace", [])]
    assert_true("status_tool_selected", ("decide", "tool_selected") in statuses)
    assert_true("status_confirmed", ("status", "next_step_confirmed") in statuses)
    assert_true("router_was_called", len(fake_client.calls) >= 2, str(len(fake_client.calls)))
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
    original_send = web_server._send_agent_message
    original_delegate = web_server._start_crawler_job_from_crawler_tool
    calls: list[dict[str, Any]] = []

    def fake_send(config: AppConfig, payload: dict[str, Any], *, from_agent: str, content: str, to_agent: str, emit: Any | None = None, metadata: dict[str, Any] | None = None, **kwargs: Any):  # noqa: ARG001
        message = web_server.make_agent_message(from_agent, content, to_agent, metadata=metadata)
        calls.append({"payload": {**payload, "delivery_target": message.metadata.get("delivery_target")}, "message": message, "question": message.content})
        delegated_question = message.content
        job = web_server.Job(id="fake-mcagent-gap-job", kind="crawler", title=delegated_question, status="queued", summary="queued")
        job.result = {"plan": {"topic": delegated_question, "delivery_target": message.metadata.get("delivery_target")}}
        return {"answer": "我是 CrawlerAgent。采集任务已启动。", "agent": "crawler_agent", "job": web_server._job_to_dict(job)}

    def fail_direct_start(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("MCagent must send AgentMessage to CrawlerAgent instead of starting crawler job directly")

    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake_client, "fake")  # type: ignore[assignment]
    web_server._send_agent_message = fake_send  # type: ignore[assignment]
    web_server._start_crawler_job_from_crawler_tool = fail_direct_start  # type: ignore[assignment]
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
        web_server._send_agent_message = original_send  # type: ignore[assignment]
        web_server._start_crawler_job_from_crawler_tool = original_delegate  # type: ignore[assignment]
        tmp.cleanup()

    assert_true("delegated", bool(calls))
    assert_equal("message_from", calls[0]["message"].from_agent, "MCagent")
    assert_equal("message_to", calls[0]["message"].to_agent, "CrawlerAgent")
    assert_equal("requested_by", result.get("delegation", {}).get("requested_by"), "user_via_mcagent")
    assert_equal("delivery_target", result.get("delegation", {}).get("delivery_target"), "MCagent/RAG")
    assert_equal("message_delivery", calls[0]["message"].metadata.get("delivery_target"), "MCagent/RAG")


def test_delegate_confirmation_can_cancel_background_job() -> None:
    tmp = tempfile.TemporaryDirectory()
    fake_client = SequencedClient(
        [
            json.dumps(
                {
                    "tool": "delegate_crawler",
                    "reason": "initially thought collection was needed",
                    "collection_target": "collect Utopia modpack data",
                    "delivery_target": "MCagent/RAG",
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "proceed": True,
                    "tool": "delegate_crawler",
                    "goal": "allow initial route decision",
                    "reason": "route confirmation accepts the selected tool",
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "proceed": False,
                    "tool": "direct_answer",
                    "suggested_tool": "direct_answer",
                    "goal": "explain that collection is not being started",
                    "reason": "confirmation cancelled the background side effect",
                },
                ensure_ascii=False,
            ),
        ]
    )
    original_selector = web_server._selected_llm_client
    original_delegate = web_server._start_crawler_job_from_crawler_tool
    calls: list[dict[str, Any]] = []

    def fake_delegate(config: AppConfig, payload: dict[str, Any], delegated_question: str, plan: dict[str, Any] | None = None):  # noqa: ARG001
        calls.append({"payload": payload, "question": delegated_question, "plan": plan})
        job = web_server.Job(id="unexpected-job", kind="crawler", title=delegated_question, status="queued", summary="queued")
        return job, True

    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake_client, "fake")  # type: ignore[assignment]
    web_server._start_crawler_job_from_crawler_tool = fake_delegate  # type: ignore[assignment]
    try:
        result = web_server._chat_impl(
            make_temp_config(Path(tmp.name)),
            {
                "agent": "mcagent_rag",
                "question": "先别采集，说明一下现在为什么不需要启动后台任务",
                "session_id": "delegate-confirmation-cancel-test",
                "model": "fake-model",
            },
        )
    finally:
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        web_server._start_crawler_job_from_crawler_tool = original_delegate  # type: ignore[assignment]
        tmp.cleanup()

    assert_equal("no_background_job", calls, [])
    statuses = [(item.get("stage"), item.get("status")) for item in result.get("trace") or []]
    assert_true("delegate_confirmed_trace", ("delegate", "next_step_confirmed") in statuses, str(statuses))
    assert_true("no_job_response", not result.get("job"), str(result.get("job")))


def test_explicit_mcagent_to_crawler_handoff_starts_job_after_agent_selects_delegate() -> None:
    tmp = tempfile.TemporaryDirectory()
    question = "\u8bf7\u5148\u68c0\u67e5\u672c\u5730\u8d44\u6599\u91cc\u4e4c\u6258\u90a6\u63a2\u9669\u4e4b\u65c5 / Utopian Journey \u6574\u5408\u5305\u8fd8\u7f3a\u54ea\u4e9b\u5185\u5bb9\uff0c\u7136\u540e\u8ba9 CrawlerAgent \u53bb\u7f51\u4e0a\u91c7\u96c6\u7f3a\u5931\u7684\u516c\u5f00\u8d44\u6599\u5e76\u5165\u5e93\u7ed9 MCagent/RAG \u4f7f\u7528\u3002"
    original_send = web_server._send_agent_message
    original_delegate = web_server._start_crawler_job_from_crawler_tool
    original_selector = web_server._selected_llm_client
    calls: list[dict[str, Any]] = []
    fake_client = SequencedClient(
        [
            json.dumps(
                {
                    "tool": "delegate_crawler",
                    "reason": "Agent chose Crawler collection with RAG delivery.",
                    "collection_target": question,
                    "delivery_target": "MCagent/RAG",
                },
                ensure_ascii=False,
            ),
            json.dumps({"proceed": True, "tool": "delegate_crawler", "reason": "confirmed"}, ensure_ascii=False),
            json.dumps({"handoff_brief": "MCagent transfers the user's collection request to CrawlerAgent.", "reason": "handoff"}, ensure_ascii=False),
        ]
    )

    def fake_send(config: AppConfig, payload: dict[str, Any], *, from_agent: str, content: str, to_agent: str, emit: Any | None = None, metadata: dict[str, Any] | None = None, **kwargs: Any):  # noqa: ARG001
        message = web_server.make_agent_message(from_agent, content, to_agent, metadata=metadata)
        calls.append({"payload": {**payload, "requested_by": message.metadata.get("requested_by"), "delivery_target": message.metadata.get("delivery_target")}, "message": message, "question": message.content})
        delegated_question = message.content
        job = web_server.Job(id="fake-fast-handoff-job", kind="crawler", title=delegated_question, status="queued", summary="queued")
        job.result = {"plan": {"topic": delegated_question, "delivery_target": message.metadata.get("delivery_target")}}
        return {"answer": "我是 CrawlerAgent。采集任务已启动。", "agent": "crawler_agent", "job": web_server._job_to_dict(job)}

    def fail_direct_start(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("MCagent must send AgentMessage to CrawlerAgent instead of starting crawler job directly")

    web_server._send_agent_message = fake_send  # type: ignore[assignment]
    web_server._start_crawler_job_from_crawler_tool = fail_direct_start  # type: ignore[assignment]
    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake_client, "fake")  # type: ignore[assignment]
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
        web_server._send_agent_message = original_send  # type: ignore[assignment]
        web_server._start_crawler_job_from_crawler_tool = original_delegate  # type: ignore[assignment]
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        tmp.cleanup()

    assert_true("delegated", bool(calls))
    assert_equal("requested_by", calls[0]["payload"].get("requested_by"), "user_via_mcagent")
    assert_equal("delivery_target", calls[0]["payload"].get("delivery_target"), "MCagent/RAG")
    assert_true("clean_target_keeps_alias", "乌托邦探险之旅 / Utopian Journey" in calls[0]["question"], calls[0]["question"])
    assert_true("clean_target_no_agent_damage", "Crawle ent" not in calls[0]["question"] and "给 / 使用" not in calls[0]["question"], calls[0]["question"])
    assert_true("has_job", result.get("job", {}).get("id") == "fake-fast-handoff-job")
    statuses = [(item.get("stage"), item.get("status")) for item in result.get("trace") or []]
    assert_true("tool_selected", ("decide", "tool_selected") in statuses, str(statuses))
    assert_true("delegate_confirmed", ("delegate", "next_step_confirmed") in statuses, str(statuses))


def test_explicit_mcagent_to_crawler_handoff_relays_before_heavy_router() -> None:
    tmp = tempfile.TemporaryDirectory()
    question = "让MCagent转达Crawler去获取乌托邦整合包资料，先问MCagent本地缺口，再采集公开网页补入RAG。"
    original_send = web_server._send_agent_message
    original_router = web_server.LlmAgentToolRouterService
    calls: list[dict[str, Any]] = []

    def fake_send(config: AppConfig, payload: dict[str, Any], *, from_agent: str, content: str, to_agent: str, metadata: dict[str, Any] | None = None, **kwargs: Any):  # noqa: ARG001
        calls.append({"payload": payload, "from_agent": from_agent, "content": content, "to_agent": to_agent, "metadata": metadata or {}, "kwargs": kwargs})
        job = web_server.Job(id="relay-job", kind="crawler", title=content, status="queued", summary="queued")
        return {"answer": "我是 CrawlerAgent。已启动后台采集任务。", "agent": "crawler_agent", "job": web_server._job_to_dict(job)}

    class ForbiddenRouter:
        def __init__(self, *args, **kwargs):  # noqa: ANN001
            raise AssertionError("explicit MCagent->Crawler relay should not enter the heavy MCagent router before sending the AgentMessage")

    web_server._send_agent_message = fake_send  # type: ignore[assignment]
    web_server.LlmAgentToolRouterService = ForbiddenRouter  # type: ignore[assignment]
    try:
        result = web_server._chat_impl(
            make_temp_config(Path(tmp.name)),
            {
                "agent": "mcagent_rag",
                "question": question,
                "session_id": "explicit-relay-light-path",
                "model": "fake-model",
            },
        )
    finally:
        web_server._send_agent_message = original_send  # type: ignore[assignment]
        web_server.LlmAgentToolRouterService = original_router  # type: ignore[assignment]
        tmp.cleanup()

    assert_equal("one_message", len(calls), 1)
    assert_equal("from_agent", calls[0]["from_agent"], "MCagent")
    assert_equal("to_agent", calls[0]["to_agent"], "CrawlerAgent")
    assert_equal("tool", calls[0]["metadata"].get("tool"), "collection_request")
    assert_equal("requested_by", calls[0]["metadata"].get("requested_by"), "user_via_mcagent")
    assert_equal("delivery_target", calls[0]["metadata"].get("delivery_target"), "MCagent/RAG")
    assert_true("content_keeps_target", "乌托邦整合包" in calls[0]["content"], calls[0]["content"])
    assert_true("answer_prefix", "MCagent 已通过 From-Content-To" in result.get("answer", ""), result.get("answer", ""))
    assert_equal("job", result.get("job", {}).get("id"), "relay-job")


def test_explicit_handoff_with_source_audit_requirements_still_relays_fast() -> None:
    question = (
        "让MCagent转达Crawler去获取 Playwright Python Trace Viewer 和网络录制相关的公开官方资料。"
        "要求：Crawler自己判断来源是否可用，记录接受/拒绝原因，保存为可引用资料；"
        "这不是Minecraft资料，不要用MC专用来源。"
    )
    assert_true("handoff_detected", web_server._user_requested_mcagent_crawler_handoff(question))
    assert_true("explicit_handoff", web_server._user_explicitly_asked_mcagent_to_tell_crawler(question))
    cleaned = web_server._clean_crawler_task_question(question)
    assert_true("cleaned_keeps_target", "Playwright Python Trace Viewer" in cleaned, cleaned)
    assert_true("cleaned_drops_relay_prefix", not cleaned.startswith("让MCagent"), cleaned)


def test_recent_crawler_audit_question_answers_history_without_new_collection() -> None:
    tmp = tempfile.TemporaryDirectory()
    question = "刚才 Crawler 采集农夫乐事时，哪些来源被接受，哪些被拒绝，为什么？是否已经入库？"
    original_delegate = web_server._start_crawler_job_from_crawler_tool
    original_selector = web_server._selected_llm_client
    original_jobs = dict(web_server.JOBS)
    original_jobs_order = list(web_server.JOBS_ORDER)
    calls: list[dict[str, Any]] = []

    def fake_delegate(config: AppConfig, payload: dict[str, Any], delegated_question: str, plan: dict[str, Any] | None = None):  # noqa: ARG001
        calls.append({"payload": payload, "question": delegated_question, "plan": plan})
        return web_server.Job(id="should-not-start", kind="crawler", title="bad"), True

    web_server._start_crawler_job_from_crawler_tool = fake_delegate  # type: ignore[assignment]
    fake_client = SequencedClient(
        [
            json.dumps({"tool": "crawler_audit", "reason": "read recent Crawler audit"}, ensure_ascii=False),
            json.dumps({"proceed": True, "tool": "crawler_audit", "reason": "confirmed"}, ensure_ascii=False),
        ]
    )
    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake_client, "fake")  # type: ignore[assignment]
    try:
        with web_server.JOBS_LOCK:
            web_server.JOBS.clear()
            web_server.JOBS_ORDER.clear()
        result = web_server._chat_impl(
            make_temp_config(Path(tmp.name)),
            {
                "agent": "mcagent_rag",
                "question": question,
                "session_id": "recent-crawler-audit",
                "model": "fake-model",
            },
        )
    finally:
        with web_server.JOBS_LOCK:
            web_server.JOBS.clear()
            web_server.JOBS.update(original_jobs)
            web_server.JOBS_ORDER[:] = original_jobs_order
        web_server._start_crawler_job_from_crawler_tool = original_delegate  # type: ignore[assignment]
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        tmp.cleanup()

    assert_equal("no_delegate_calls", len(calls), 0)
    assert_true("history_answer", "不会新开采集任务" in str(result.get("answer") or ""))
    statuses = [(item.get("stage"), item.get("status")) for item in result.get("trace") or []]
    assert_true("audit_trace", ("answer", "recent_crawler_audit") in statuses, str(statuses))


def test_recent_crawler_audit_question_matches_create_instead_of_higher_activity_job() -> None:
    original_jobs = dict(web_server.JOBS)
    original_jobs_order = list(web_server.JOBS_ORDER)

    create_job = web_server.Job(id="create-job", kind="crawler", title="Create crawl", status="succeeded", summary="done")
    create_job.result = {
        "plan": {"topic": "Create / \u673a\u68b0\u52a8\u529b"},
        "ingest": {"stats": {"documents_loaded": 4}},
        "tasks": [
            {
                "source": "modrinth",
                "query": "Create",
                "returncode": 0,
                "ingest_deferred": True,
                "manifest_stats": {"records": 2},
                "topic_validation": {"matched": True},
            },
            {
                "source": "fetch_url",
                "query": "https://wiki.createmod.net/",
                "returncode": 0,
                "ingest_deferred": True,
                "manifest_stats": {"records": 1},
                "topic_validation": {"matched": True},
            },
        ],
    }
    stopped_create_job = web_server.Job(id="stopped-create-job", kind="crawler", title="For the latest Create crawler job", status="stopped", summary="Read latest Create crawl ledger")
    stopped_create_job.result = {
        "plan": {"topic": "For the latest Create crawler job"},
        "tasks": [
            {
                "source": "mcagent_context",
                "query": "For the latest Create crawler job",
                "returncode": 0,
                "manifest_stats": {"records": 1},
                "records_pending_review": True,
            },
            {
                "source": "read_local_file",
                "query": "crawl_ledger.jsonl",
                "returncode": 1,
                "manifest_stats": {"records": 0},
                "observation": {"status": "blocked"},
            },
        ],
    }
    farmers_job = web_server.Job(id="farmers-job", kind="crawler", title="Farmer's Delight crawl", status="succeeded", summary="done")
    farmers_job.result = {
        "plan": {"topic": "\u519c\u592b\u4e50\u4e8b / Farmer's Delight"},
        "ingest": {"stats": {"documents_loaded": 8}},
        "tasks": [
            {
                "source": "fetch_url",
                "query": "https://www.mcmod.cn/class/2820.html",
                "returncode": 0,
                "ingest_deferred": True,
                "manifest_stats": {"records": 1},
                "topic_validation": {"matched": True},
            },
            {
                "source": "mcmod",
                "query": "\u519c\u592b\u4e50\u4e8b related pages",
                "returncode": 0,
                "manifest_stats": {"records": 1},
                "topic_validation": {"matched": True},
                "search_results": [{"title": "[CCK]\u673a\u68b0\u52a8\u529b\uff1a\u4e2d\u592e\u53a8\u623f (Create: Central Kitchen)"}],
            },
            {
                "source": "web_discovery",
                "query": "Farmer's Delight wiki",
                "returncode": 1,
                "manifest_stats": {"records": 0},
                "observation": {"status": "empty"},
            },
            {
                "source": "modrinth",
                "query": "farmers-delight",
                "returncode": 1,
                "manifest_stats": {"records": 0},
                "observation": {"status": "empty"},
            },
        ],
    }
    try:
        with web_server.JOBS_LOCK:
            web_server.JOBS.clear()
            web_server.JOBS_ORDER.clear()
            web_server.JOBS["stopped-create-job"] = stopped_create_job
            web_server.JOBS["create-job"] = create_job
            web_server.JOBS["farmers-job"] = farmers_job
            web_server.JOBS_ORDER.extend(["stopped-create-job", "create-job", "farmers-job"])
        answer = web_server._recent_crawler_audit_answer(
            "\u521a\u624d Crawler \u91c7\u96c6 Create / \u673a\u68b0\u52a8\u529b \u65f6\uff0c\u54ea\u4e9b\u6765\u6e90\u88ab\u63a5\u53d7\uff1f"
        )
    finally:
        with web_server.JOBS_LOCK:
            web_server.JOBS.clear()
            web_server.JOBS.update(original_jobs)
            web_server.JOBS_ORDER[:] = original_jobs_order

    assert_true("has_answer", answer is not None)
    assert_equal("matched_create_job", (answer or {}).get("job", {}).get("id"), "create-job")
    assert_true("mentions_create", "Create" in str((answer or {}).get("answer") or ""))


def test_modrinth_plain_mod_task_does_not_parse_modpack_contents() -> None:
    command = web_server._round_command(
        "modrinth",
        {
            "query": "\u519c\u592b\u4e50\u4e8b / Farmer's Delight",
            "reason": "\u641c\u7d22\u6a21\u7ec4\u9879\u76ee\u5143\u6570\u636e\uff0c\u7531 Crawler \u81ea\u5df1\u5224\u65ad\u76ee\u6807\u7c7b\u578b",
            "mods": 16,
            "modpacks": 5,
            "resourcepacks": 3,
            "shaders": 1,
        },
    )
    assert_true("no_modpack_contents", "--include-modpack-contents" not in command, str(command))


def test_known_modrinth_project_skips_are_reusable_existing_evidence() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        previous = root / "mod_farmers-delight.md"
        previous.write_text("# Farmer's Delight\n\nFarm cooking mod.\n", encoding="utf-8")
        run_dir = root / "modrinth_agent" / "run"
        run_dir.mkdir(parents=True)
        (run_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "records": [],
                    "skipped": [
                        {
                            "title": "Farmer's Delight",
                            "slug": "farmers-delight",
                            "project_type": "mod",
                            "url": "https://modrinth.com/mod/farmers-delight",
                            "reason": "known_project",
                            "previous_path": str(previous),
                        }
                    ],
                    "errors": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        original_judge = web_server._crawler_llm_record_relevance
        web_server._crawler_llm_record_relevance = lambda *_args, **_kwargs: {  # type: ignore[assignment]
            "matched": True,
            "reason": "direct",
            "matched_indexes": [0],
            "rejected_indexes": [],
            "cleanup_action": "keep",
            "next_action": "reuse_existing_modrinth_project",
            "judge": "Crawler LLM",
        }
        try:
            review = web_server._crawler_reusable_duplicate_evidence(
                str(run_dir),
                "农夫乐事 / Farmer's Delight",
                "farmer's delight",
                {"topic": "农夫乐事 / Farmer's Delight"},
            )
        finally:
            web_server._crawler_llm_record_relevance = original_judge  # type: ignore[assignment]
    assert_equal("matched", review.get("matched"), True)
    assert_equal("reused_title", review["records"][0]["title"], "Farmer's Delight")
    assert_equal("reused_path", review["records"][0]["path"], str(previous))


def test_modrinth_slug_query_is_direct_project_candidate() -> None:
    scripts_dir = str(ROOT / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    from scripts.fetch_modrinth_seed import direct_project_ref, possible_project_slug  # noqa: PLC0415

    assert_equal("explicit_project_ref", direct_project_ref("project: farmers-delight"), "farmers-delight")
    assert_equal("url_project_ref", direct_project_ref("https://modrinth.com/mod/farmers-delight"), "farmers-delight")
    assert_equal("slug_candidate", possible_project_slug("farmers-delight"), "farmers-delight")
    assert_equal("free_text_not_slug", possible_project_slug("Farmer's Delight"), "")


def test_modrinth_explicit_modpack_manifest_task_can_parse_contents() -> None:
    command = web_server._round_command(
        "modrinth",
        {
            "query": "Utopian Journey modpack",
            "reason": "整合包 manifest / modlist / .mrpack contents",
            "mods": 5,
            "modpacks": 5,
        },
    )
    assert_true("has_modpack_contents", "--include-modpack-contents" in command, str(command))


def test_direct_crawler_review_cannot_correct_direct_answer_to_unselected_delegation() -> None:
    tmp = tempfile.TemporaryDirectory()
    fake_client = SequencedClient(
        [
            '{"tool":"direct_answer","reason":"CrawlerAgent cannot ask MCagent directly.","collection_target":"问下MCAgent乌托邦整合包还缺哪些东西 你去网上找补给他","delivery_target":"human"}',
            '{"proceed":true,"tool":"direct_answer","reason":"mistaken direct answer"}',
            '{"missing_side_effect":true,"tool":"delegate_crawler","action":"execute_selected_tool","reason":"用户要求先问 MCagent 再继续采集补给他，direct_answer 没有执行跨 Agent 沟通和后台采集。","collection_target":"问下MCAgent乌托邦整合包还缺哪些东西，然后去网上找补给他","delivery_target":"MCagent/RAG"}',
            '{"proceed":true,"tool":"delegate_crawler","reason":"confirmed corrected delegation"}',
            '{"handoff_brief":"CrawlerAgent 误选直接回答后，经完整性审查改为通过 AgentMessage 执行采集任务：先询问 MCagent 缺口，再补充乌托邦整合包资料。","reason":"handoff"}',
        ]
    )
    original_selector = web_server._selected_llm_client
    original_delegate = web_server._start_crawler_job_from_crawler_tool
    original_retrieve = web_server.RagRetrievalService.retrieve
    calls: list[dict[str, Any]] = []

    def fake_delegate(config: AppConfig, payload: dict[str, Any], question: str, plan: dict[str, Any] | None = None):  # noqa: ARG001
        calls.append({"payload": payload, "question": question, "plan": plan})
        job = web_server.Job(id="fake-crawler-job", kind="crawler", title=question, status="queued", summary="queued")
        job.result = {"plan": {"topic": question, "delivery_target": payload.get("delivery_target")}}
        return job, True

    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake_client, "fake")  # type: ignore[assignment]
    web_server._start_crawler_job_from_crawler_tool = fake_delegate  # type: ignore[assignment]
    web_server.RagRetrievalService.retrieve = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("direct_answer path should return without retrieval"))  # type: ignore[assignment]
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
        web_server._start_crawler_job_from_crawler_tool = original_delegate  # type: ignore[assignment]
        web_server.RagRetrievalService.retrieve = original_retrieve  # type: ignore[assignment]
        tmp.cleanup()

    statuses = [(step["stage"], step["status"]) for step in result.get("trace", [])]
    assert_true("direct_answer_selected", ("decide", "tool_selected") in statuses, str(statuses))
    assert_true("completeness_gap_found", ("plan", "route_completeness_gap") in statuses, str(statuses))
    assert_true("not_executed_trace", ("decide", "direct_answer_missing_side_effect_not_executed") in statuses, str(statuses))
    assert_true("no_unselected_delegate", not calls, str(calls))
    assert_equal("agent_identity", result.get("agent"), "crawler_agent")
    assert_true("no_job_returned", not result.get("job"), str(result.get("job")))


def test_direct_crawler_delegate_choice_starts_crawler_job_without_forced_context_rewrite() -> None:
    tmp = tempfile.TemporaryDirectory()
    fake_client = SequencedClient(
        [
            '{"tool":"delegate_crawler","reason":"check gaps then collect","collection_target":"先检查MCagent本地资料中关于乌托邦整合包缺失的内容，然后去网上找补给他","delivery_target":"MCagent/RAG","action_plan":[{"step":1,"tool":"delegate_crawler","goal":"collect"}]}',
            '{"proceed":true,"tool":"delegate_crawler","reason":"confirmed"}',
            '{"handoff_brief":"用户直接委托 CrawlerAgent：先参考 MCagent/RAG 空缺，再采集乌托邦整合包缺失资料。","reason":"handoff"}',
        ]
    )
    original_selector = web_server._selected_llm_client
    original_delegate = web_server._start_crawler_job_from_crawler_tool
    original_retrieve = web_server.RagRetrievalService.retrieve
    calls: list[dict[str, Any]] = []

    def fake_delegate(config: AppConfig, payload: dict[str, Any], question: str, plan: dict[str, Any] | None = None):  # noqa: ARG001
        calls.append({"payload": payload, "question": question, "plan": plan})
        job = web_server.Job(id="fake-crawler-job-3", kind="crawler", title=question, status="queued", summary="queued")
        job.result = {"plan": {"topic": question, "delivery_target": payload.get("delivery_target")}}
        return job, True

    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake_client, "fake")  # type: ignore[assignment]
    web_server._start_crawler_job_from_crawler_tool = fake_delegate  # type: ignore[assignment]
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
        web_server._start_crawler_job_from_crawler_tool = original_delegate  # type: ignore[assignment]
        web_server.RagRetrievalService.retrieve = original_retrieve  # type: ignore[assignment]
        tmp.cleanup()

    statuses = [(step["stage"], step["status"]) for step in result.get("trace", [])]
    assert_true("tool_selected", ("decide", "tool_selected") in statuses, str(statuses))
    assert_true("delegate_confirmed", ("delegate", "next_step_confirmed") in statuses, str(statuses))
    assert_true("delegated", bool(calls))
    assert_equal("agent_identity", result.get("agent"), "crawler_agent")
    assert_equal("requested_by", result.get("delegation", {}).get("requested_by"), "user")
    assert_equal("delivery_target", result.get("delegation", {}).get("delivery_target"), "MCagent/RAG")
    assert_true("crawler_voice", str(result.get("answer") or "").startswith("我是 CrawlerAgent。"))
    assert_true("no_self_handoff_voice", "转交给 CrawlerAgent" not in str(result.get("answer") or ""))
    assert_true("clean_collection_target", "CrawlerAgent 应" not in calls[0]["question"] and "用户原始目标" not in calls[0]["question"])
    summary = calls[0]["payload"].get("session_summary") or {}
    assert_true("no_forced_planning_instruction", "mcagent_context" not in str(summary.get("planning_instruction") or ""))


def test_crawler_collection_request_message_does_not_force_tool_choice() -> None:
    tmp = tempfile.TemporaryDirectory()
    fake_client = SequencedClient(
        [
            '{"tool":"answer","reason":"mistakenly treated delegated collection request as answer","collection_target":"Utopia gap fill","delivery_target":"MCagent/RAG"}',
            '{"proceed":true,"tool":"answer","reason":"confirmed mistaken route"}',
            '{"handoff_brief":"MCagent sent a collection request to CrawlerAgent through AgentMessage.","reason":"handoff"}',
        ]
    )
    original_selector = web_server._selected_llm_client
    original_delegate = web_server._start_crawler_job_from_crawler_tool
    calls: list[dict[str, Any]] = []

    def fake_delegate(config: AppConfig, payload: dict[str, Any], question: str, plan: dict[str, Any] | None = None):  # noqa: ARG001
        calls.append({"payload": payload, "question": question, "plan": plan})
        job = web_server.Job(id="collection-request-job", kind="crawler", title=question, status="queued", summary="queued")
        job.result = {"plan": {"topic": question, "delivery_target": payload.get("delivery_target")}}
        return job, True

    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake_client, "fake")  # type: ignore[assignment]
    web_server._start_crawler_job_from_crawler_tool = fake_delegate  # type: ignore[assignment]
    try:
        result = web_server._send_agent_message(
            make_temp_config(Path(tmp.name)),
            {
                "session_id": "collection-request-contract",
                "model": "fake-model",
                "delivery_target": "MCagent/RAG",
                "requested_by": "user_via_mcagent",
            },
            from_agent="MCagent",
            content="Utopia gap fill",
            to_agent="CrawlerAgent",
            intent="collection_request",
            conversation_id="collection-request-contract",
            metadata={"tool": "delegate_crawler", "delivery_target": "MCagent/RAG", "requested_by": "user_via_mcagent"},
        )
    finally:
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        web_server._start_crawler_job_from_crawler_tool = original_delegate  # type: ignore[assignment]
        tmp.cleanup()

    assert_true("job_not_forced", not calls, str(calls))
    assert_true("no_job_response", not result.get("job"), str(result))
    statuses = [(step.get("stage"), step.get("status")) for step in result.get("trace") or []]
    assert_true("message_context_trace", ("message", "collection_request_received_for_agent_decision") in statuses, str(statuses))


def test_crawler_collection_request_preserves_mcagent_context_choice_without_forcing_delegate() -> None:
    tmp = tempfile.TemporaryDirectory()
    fake_client = SequencedClient(
        [
            '{"tool":"mcagent_context","reason":"CrawlerAgent chooses to ask MCagent for local gaps first.","collection_target":"Utopia gap fill","delivery_target":"MCagent/RAG"}',
            '{"proceed":true,"tool":"mcagent_context","reason":"confirmed by CrawlerAgent"}',
        ]
    )
    original_selector = web_server._selected_llm_client
    original_delegate = web_server._start_crawler_job_from_crawler_tool
    original_retriever = web_server.RagRetrievalService.retrieve
    calls: list[dict[str, Any]] = []

    def fake_delegate(config: AppConfig, payload: dict[str, Any], question: str, plan: dict[str, Any] | None = None):  # noqa: ARG001
        calls.append({"payload": payload, "question": question, "plan": plan})
        job = web_server.Job(id="unexpected-forced-job", kind="crawler", title=question, status="queued", summary="queued")
        return job, True

    def fake_retrieve(self, config: AppConfig, *, agent: str, original_question: str, question: str, session_summary: dict[str, Any], preparation: Any, use_planner: bool, add_trace: Any):  # noqa: ANN001, ARG002
        return SimpleNamespace(retrieval_plan={}, rough_results=[], selected=[])

    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake_client, "fake")  # type: ignore[assignment]
    web_server._start_crawler_job_from_crawler_tool = fake_delegate  # type: ignore[assignment]
    web_server.RagRetrievalService.retrieve = fake_retrieve  # type: ignore[assignment]
    try:
        result = web_server._send_agent_message(
            make_temp_config(Path(tmp.name)),
            {
                "session_id": "collection-request-crawler-selects-context-only",
                "model": "fake-model",
                "delivery_target": "MCagent/RAG",
                "requested_by": "user_via_mcagent",
            },
            from_agent="MCagent",
            content="Utopia gap fill",
            to_agent="CrawlerAgent",
            intent="collection_request",
            conversation_id="collection-request-crawler-selects-context-only",
            metadata={"tool": "collection_request", "delivery_target": "MCagent/RAG", "requested_by": "user_via_mcagent"},
        )
    finally:
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        web_server._start_crawler_job_from_crawler_tool = original_delegate  # type: ignore[assignment]
        web_server.RagRetrievalService.retrieve = original_retriever  # type: ignore[assignment]
        tmp.cleanup()

    assert_true("job_not_forced_after_mcagent_context_choice", not calls, str(calls))
    assert_true("no_job_response", not result.get("job"), str(result))
    statuses = [(step.get("stage"), step.get("status")) for step in result.get("trace") or []]
    assert_true("message_context_trace", ("message", "collection_request_received_for_agent_decision") in statuses, str(statuses))
    assert_true("mcagent_context_selected", ("decide", "mcagent_context_selected") in statuses, str(statuses))


def test_crawler_collection_request_message_starts_job_after_crawler_selects_delegate() -> None:
    tmp = tempfile.TemporaryDirectory()
    fake_client = SequencedClient(
        [
            '{"tool":"delegate_crawler","reason":"CrawlerAgent accepts the received collection request and chooses background collection.","collection_target":"Utopia gap fill","delivery_target":"MCagent/RAG"}',
            '{"proceed":true,"tool":"delegate_crawler","reason":"confirmed by CrawlerAgent"}',
            '{"handoff_brief":"CrawlerAgent received MCagent collection request and chose to collect public evidence for MCagent/RAG.","reason":"handoff"}',
        ]
    )
    original_selector = web_server._selected_llm_client
    original_delegate = web_server._start_crawler_job_from_crawler_tool
    calls: list[dict[str, Any]] = []

    def fake_delegate(config: AppConfig, payload: dict[str, Any], question: str, plan: dict[str, Any] | None = None):  # noqa: ARG001
        calls.append({"payload": payload, "question": question, "plan": plan})
        job = web_server.Job(id="collection-request-job", kind="crawler", title=question, status="queued", summary="queued")
        job.result = {"plan": {"topic": question, "delivery_target": payload.get("delivery_target")}}
        return job, True

    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake_client, "fake")  # type: ignore[assignment]
    web_server._start_crawler_job_from_crawler_tool = fake_delegate  # type: ignore[assignment]
    try:
        result = web_server._send_agent_message(
            make_temp_config(Path(tmp.name)),
            {
                "session_id": "collection-request-crawler-selects-delegate",
                "model": "fake-model",
                "delivery_target": "MCagent/RAG",
                "requested_by": "user_via_mcagent",
            },
            from_agent="MCagent",
            content="Utopia gap fill",
            to_agent="CrawlerAgent",
            intent="collection_request",
            conversation_id="collection-request-crawler-selects-delegate",
            metadata={"tool": "delegate_crawler", "delivery_target": "MCagent/RAG", "requested_by": "user_via_mcagent"},
        )
    finally:
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        web_server._start_crawler_job_from_crawler_tool = original_delegate  # type: ignore[assignment]
        tmp.cleanup()

    assert_true("job_started_after_crawler_choice", bool(calls))
    assert_equal("crawler_agent_started_own_tool", calls[0]["payload"].get("agent"), "crawler_agent")
    assert_true("job_response", bool(result.get("job", {}).get("id")), str(result))
    assert_equal("delegation_delivery", result.get("delegation", {}).get("delivery_target"), "MCagent/RAG")
    statuses = [(step.get("stage"), step.get("status")) for step in result.get("trace") or []]
    assert_true("message_context_trace", ("message", "collection_request_received_for_agent_decision") in statuses, str(statuses))
    assert_true("tool_selected_trace", ("decide", "tool_selected") in statuses, str(statuses))

def test_direct_crawler_planned_workflow_preserves_selected_action_plan_for_job() -> None:
    tmp = tempfile.TemporaryDirectory()
    fake_client = SequencedClient(
        [
            '{"tool":"planned_workflow","reason":"ask MCagent then collect","collection_target":"根据 MCagent/RAG 对乌托邦整合包的缺口，去网上补齐资料","delivery_target":"MCagent/RAG","action_plan":[{"step":1,"tool":"mcagent_context","goal":"询问 MCagent/RAG 本地已有证据和缺口"},{"step":2,"tool":"delegate_crawler","goal":"启动后台采集并交付给 MCagent/RAG"}]}',
            '{"proceed":true,"tool":"planned_workflow","reason":"confirmed"}',
            '{"handoff_brief":"用户直接委托 CrawlerAgent：先询问 MCagent/RAG 缺口，再采集乌托邦整合包资料。","reason":"handoff"}',
        ]
    )
    original_selector = web_server._selected_llm_client
    original_delegate = web_server._start_crawler_job_from_crawler_tool
    original_retrieve = web_server.RagRetrievalService.retrieve
    calls: list[dict[str, Any]] = []

    def fake_delegate(config: AppConfig, payload: dict[str, Any], question: str, plan: dict[str, Any] | None = None):  # noqa: ARG001
        calls.append({"payload": payload, "question": question, "plan": plan})
        job = web_server.Job(id="fake-crawler-job-plan", kind="crawler", title=question, status="queued", summary="queued")
        job.result = {"plan": {"topic": question, "delivery_target": payload.get("delivery_target")}}
        return job, True

    def fail_retrieve(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("CrawlerAgent planned_workflow should start a background job, not run chat-turn RAG retrieval")

    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake_client, "fake")  # type: ignore[assignment]
    web_server._start_crawler_job_from_crawler_tool = fake_delegate  # type: ignore[assignment]
    web_server.RagRetrievalService.retrieve = fail_retrieve  # type: ignore[assignment]
    try:
        result = web_server._chat_impl(
            make_temp_config(Path(tmp.name)),
            {
                "agent": "crawler_agent",
                "question": "问下MCAgent乌托邦整合包还缺哪些东西 你去网上找补给他",
                "session_id": "direct-crawler-planned-workflow-selected-plan-test",
                "model": "fake-model",
            },
        )
    finally:
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        web_server._start_crawler_job_from_crawler_tool = original_delegate  # type: ignore[assignment]
        web_server.RagRetrievalService.retrieve = original_retrieve  # type: ignore[assignment]
        tmp.cleanup()

    assert_true("delegated", bool(calls))
    assert_equal("agent_identity", result.get("agent"), "crawler_agent")
    summary = calls[0]["payload"].get("session_summary") or {}
    selected = summary.get("selected_action_plan") or []
    assert_true("selected_plan_preserved", any(isinstance(item, dict) and item.get("tool") == "mcagent_context" for item in selected), str(selected))
    assert_true("answer_crawler_voice", str(result.get("answer") or "").startswith("我是 CrawlerAgent。"))


def test_direct_crawler_mcagent_context_step_with_delegate_starts_background_job() -> None:
    tmp = tempfile.TemporaryDirectory()
    fake_client = SequencedClient(
        [
            '{"tool":"mcagent_context","reason":"ask MCagent first","collection_target":"乌托邦整合包缺口","delivery_target":"MCagent/RAG","action_plan":[{"step":1,"tool":"mcagent_context","goal":"询问 MCagent/RAG 本地已有证据和缺口"},{"step":2,"tool":"delegate_crawler","goal":"启动后台采集并交付给 MCagent/RAG"}]}',
            '{"proceed":true,"tool":"mcagent_context","reason":"confirmed"}',
            '{"handoff_brief":"CrawlerAgent 先问 MCagent，再继续采集。","reason":"handoff"}',
        ]
    )
    original_selector = web_server._selected_llm_client
    original_delegate = web_server._start_crawler_job_from_crawler_tool
    original_retrieve = web_server.RagRetrievalService.retrieve
    calls: list[dict[str, Any]] = []

    def fake_delegate(config: AppConfig, payload: dict[str, Any], question: str, plan: dict[str, Any] | None = None):  # noqa: ARG001
        calls.append({"payload": payload, "question": question, "plan": plan})
        job = web_server.Job(id="fake-crawler-mcagent-context-job", kind="crawler", title=question, status="queued", summary="queued")
        job.result = {"plan": {"topic": question, "delivery_target": payload.get("delivery_target")}}
        return job, True

    def fail_retrieve(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("CrawlerAgent mcagent_context+delegate workflow must start a background job, not answer with chat-turn RAG")

    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake_client, "fake")  # type: ignore[assignment]
    web_server._start_crawler_job_from_crawler_tool = fake_delegate  # type: ignore[assignment]
    web_server.RagRetrievalService.retrieve = fail_retrieve  # type: ignore[assignment]
    try:
        result = web_server._chat_impl(
            make_temp_config(Path(tmp.name)),
            {
                "agent": "crawler_agent",
                "question": "问下MCAgent乌托邦整合包还缺哪些东西 你去网上找补给他",
                "session_id": "direct-crawler-mcagent-context-plus-delegate-test",
                "model": "fake-model",
            },
        )
    finally:
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        web_server._start_crawler_job_from_crawler_tool = original_delegate  # type: ignore[assignment]
        web_server.RagRetrievalService.retrieve = original_retrieve  # type: ignore[assignment]
        tmp.cleanup()

    assert_true("delegated", bool(calls))
    assert_true("job_response", bool(result.get("job", {}).get("id")), str(result))
    statuses = [(step.get("stage"), step.get("status")) for step in result.get("trace") or []]
    assert_true("deferred_context_step", ("decide", "mcagent_context_deferred_to_background_workflow") in statuses, str(statuses))


def test_crawler_job_can_execute_mcagent_context_tool() -> None:
    tmp = tempfile.TemporaryDirectory()
    original_send = web_server._send_agent_message
    calls: list[dict[str, Any]] = []

    def fake_send(config: AppConfig, payload: dict[str, Any], *, from_agent: str, content: str, to_agent: str, emit: Any | None = None, conversation_id: str = "", **kwargs: Any) -> dict[str, Any]:  # noqa: ARG001
        message = web_server.make_agent_message(from_agent, content, to_agent, conversation_id=conversation_id)
        calls.append({"payload": payload, "message": message})
        reply = web_server.make_agent_message(
            "MCagent",
            "MCagent 回复 CrawlerAgent：本地已有基础介绍，缺少完整模组列表、任务线和 Boss 攻略。",
            "CrawlerAgent",
            intent="agent_reply",
            conversation_id=message.conversation_id,
            reply_to=message.message_id,
        )
        return {
            "answer": reply.content,
            "agent": "mcagent_rag",
            "agent_message": reply.to_dict(),
            "sources": [
                {
                    "rank": 1,
                    "score": 9.5,
                    "title": "乌托邦探险之旅本地资料",
                    "source_path": str(Path(tmp.name) / "utopia.md"),
                    "url": "https://example.test/utopia",
                    "text": "乌托邦探险之旅已有基础介绍，但缺少完整模组列表、任务线和 Boss 攻略。",
                    "metadata": {},
                }
            ],
            "evidence": {"verdict": "insufficient", "reasons": ["缺少完整模组列表", "缺少任务线"]},
            "trace": [
                {"stage": "message", "status": "received", "detail": message.to_dict()},
                {"stage": "decide", "status": "tool_selected", "detail": {"tool": "answer"}},
                {"stage": "retrieve", "status": "next_step_confirmed", "detail": {"tool": "local_rag_search"}},
            ],
        }

    web_server._send_agent_message = fake_send  # type: ignore[assignment]
    try:
        result = web_server._run_mcagent_context_tool(
            make_temp_config(Path(tmp.name)),
            {"query": "乌托邦整合包", "question": "问下MCAgent乌托邦整合包还缺哪些东西"},
            {"delivery_target": "MCagent/RAG"},
            {"gaps": ["完整模组列表", "任务线"]},
        )
    finally:
        web_server._send_agent_message = original_send  # type: ignore[assignment]
        tmp.cleanup()

    try:
        assert_equal("source", result["source"], "mcagent_context")
        assert_equal("returncode", result["returncode"], 0)
        assert_true("used_message_bus", bool(calls))
        assert_equal("request_tuple", calls[0]["message"].to_tuple()[0::2], ("CrawlerAgent", "MCagent"))
        assert_equal("target_agent_payload", calls[0]["payload"].get("agent"), None)
        assert_true("mcagent_answer", "CrawlerAgent" in str(result.get("mcagent_answer") or ""))
        assert_true("has_gap_summary", "完整模组列表" in str(result.get("mcagent_gap_summary") or ""))
        assert_equal("transport", result.get("transport"), "_send_agent_message")
        export_dir = Path(str(result.get("export_dir") or ""))
        manifest = json.loads((export_dir / "manifest.json").read_text(encoding="utf-8"))
        assert_equal("manifest_source", manifest["source"], "mcagent_context")
        assert_equal("inter_agent_from", manifest["inter_agent"]["from_agent"], "CrawlerAgent")
        assert_equal("inter_agent_to", manifest["inter_agent"]["to_agent"], "MCagent")
        assert_equal("inter_agent_transport", manifest["inter_agent"]["transport"], "_send_agent_message")
        assert_true("reply_persisted", "CrawlerAgent" in manifest["inter_agent"]["reply"])
        assert_true("mcagent_received_trace", any(item.get("stage") == "message" and item.get("status") == "received" for item in manifest.get("mcagent_trace") or []))
        records = manifest.get("records") or []
        assert_true("manifest_records", len(records) >= 1)
        assert_true("manifest_has_reply_record", any(str(item.get("title") or "") == "MCagent Reply To CrawlerAgent" for item in records if isinstance(item, dict)))
        assert_true(
            "manifest_exposes_local_source_path",
            any(
                isinstance(item, dict)
                and str((item.get("metadata") or {}).get("record_role") or "") == "mcagent_local_source"
                and str(item.get("path") or "").endswith("utopia.md")
                for item in records
            ),
        )
        assert_true("result_exposes_local_source_paths", any(str(path).endswith("utopia.md") for path in result.get("mcagent_source_paths") or []))
    finally:
        if result.get("export_dir"):
            shutil.rmtree(str(result["export_dir"]), ignore_errors=True)


def test_mcagent_context_request_does_not_recursively_delegate_crawler() -> None:
    tmp = tempfile.TemporaryDirectory()
    fake_client = SequencedClient(
        [
            '{"tool":"planned_workflow","reason":"inspect local then delegate","collection_target":"乌托邦整合包资料缺口","delivery_target":"MCagent/RAG","action_plan":[{"step":1,"tool":"local_corpus_inventory","goal":"盘点本地资料"},{"step":2,"tool":"delegate_crawler","goal":"让 Crawler 补齐资料"}]}',
            '{"proceed":true,"tool":"planned_workflow","reason":"confirmed"}',
        ]
    )
    original_selector = web_server._selected_llm_client
    original_delegate = web_server._start_crawler_job_from_crawler_tool
    calls: list[dict[str, Any]] = []

    def fake_delegate(config: AppConfig, payload: dict[str, Any], question: str, plan: dict[str, Any] | None = None):  # noqa: ARG001
        calls.append({"payload": payload, "question": question, "plan": plan})
        job = web_server.Job(id="should-not-start", kind="crawler", title=question, status="queued", summary="queued")
        return job, True

    web_server._selected_llm_client = lambda *_args, **_kwargs: (fake_client, "fake")  # type: ignore[assignment]
    web_server._start_crawler_job_from_crawler_tool = fake_delegate  # type: ignore[assignment]
    try:
        result = web_server._send_agent_message(
            make_temp_config(Path(tmp.name)),
            {"session_id": "context-only-no-recursive-delegate", "model": "fake-model"},
            from_agent="CrawlerAgent",
            content="请检查乌托邦整合包本地已有证据和缺口，然后告诉 CrawlerAgent 下一步补什么。",
            to_agent="MCagent",
            intent="mcagent_context_request",
            conversation_id="context-only-no-recursive-delegate",
            metadata={"tool": "mcagent_context"},
        )
    finally:
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        web_server._start_crawler_job_from_crawler_tool = original_delegate  # type: ignore[assignment]
        tmp.cleanup()

    assert_equal("no_recursive_delegate", calls, [])
    assert_true("suppressed_trace", any(step.get("stage") == "delegate" and step.get("status") == "suppressed_for_context_reply" for step in result.get("trace") or []))
    reply = result.get("agent_message") or {}
    assert_equal("reply_to_crawler", (reply.get("from_agent"), reply.get("to_agent")), ("MCagent", "CrawlerAgent"))


def test_mcagent_context_tool_timeout_returns_objective_blocker() -> None:
    original_inner = web_server._run_mcagent_context_tool_inner
    original_timeout = web_server.DEFAULT_MCAGENT_CONTEXT_TIMEOUT_SECONDS

    def slow_inner(*args, **kwargs):  # noqa: ANN002, ANN003
        time.sleep(0.2)
        return {"source": "mcagent_context", "returncode": 0}

    web_server._run_mcagent_context_tool_inner = slow_inner  # type: ignore[assignment]
    web_server.DEFAULT_MCAGENT_CONTEXT_TIMEOUT_SECONDS = 0.05  # type: ignore[assignment]
    started = time.time()
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
    assert_true("returns_without_waiting_for_slow_inner", time.time() - started < 0.18)


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
        is_streaming = False

        def __init__(self) -> None:
            self.config = SimpleNamespace(paths=SimpleNamespace(db_path=Path("test.sqlite")))
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


def test_pseudo_delegate_call_in_final_answer_is_removed_without_late_side_effect() -> None:
    question = "现在乌托邦整合包你本地还缺哪些资料，列出来，然后让 Crawler 去补充。"
    selected = [
        SearchResult(
            rank=1,
            score=9.0,
            chunk_id=1,
            document_id=1,
            chunk_index=0,
            title="乌托邦探险之旅本地资料",
            source_path="D:/magic/MC_Agent/data/crawler_exports/utopia/local.md",
            url="https://example.test/utopia",
            text="乌托邦探险之旅已有版本、下载地址，但缺少玩法路线和完整任务线。",
            metadata={},
        )
    ]

    class FakeRun:
        original_question = ""
        question = ""
        agent = "mcagent_rag"
        model = "fake-model"
        temperature = 0.0
        max_tokens = 400
        is_streaming = False

        def __init__(self) -> None:
            self.original_question = question
            self.question = question
            self.config = SimpleNamespace(paths=SimpleNamespace(db_path=Path("test.sqlite")))
            self.payload = {"agent": self.agent, "question": self.question, "session_id": "pseudo-delegate"}
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
                tool_decision={"tool": "answer", "reason": "incorrect answer route"},
                route_intent="answer",
                action_plan=[],
                rag_focus="乌托邦整合包资料缺口",
                planned_workflow=False,
                planned_delegate=False,
            )

        def confirm_next_step(self, *args, **kwargs):
            return {"proceed": True, "tool": kwargs.get("proposed_tool"), "reason": "confirmed"}

    class FakeRag:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def prepare(self, *args, **kwargs):
            return SimpleNamespace(evidence_question="乌托邦整合包资料缺口", rough_k=8, final_k=6)

        def retrieve(self, *args, **kwargs):
            return SimpleNamespace(retrieval_plan=None, rough_results=selected, selected=selected)

    class FakeEvidenceWorkflow:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def select(self, *args, **kwargs):
            return SimpleNamespace(selected=selected, report=SimpleNamespace(verdict="ok", selected_count=1, reasons=[], to_dict=lambda: {"verdict": "ok"}))

    fake_client = SequencedClient(
        [
            json.dumps({"missing_side_effect": False, "action": "allow", "reason": "local RAG route can answer first"}, ensure_ascii=False),
            "本地已有版本和下载线索；缺少玩法路线、任务线和毕业目标。\n\ndelegate_crawler(\"请补充乌托邦整合包玩法路线、任务线和毕业目标资料\")",
            json.dumps(
                {
                    "violation": True,
                    "tool": "delegate_crawler",
                    "action": "execute_selected_tool",
                    "reason": "final answer wrote a tool call as text and the user asked to let Crawler fill gaps",
                    "collection_target": "请补充乌托邦整合包玩法路线、任务线和毕业目标资料",
                },
                ensure_ascii=False,
            ),
        ]
    )
    calls: list[dict[str, Any]] = []

    def fake_send(config: AppConfig, payload: dict[str, Any], *, from_agent: str, content: str, to_agent: str, emit: Any | None = None, metadata: dict[str, Any] | None = None, **kwargs: Any):  # noqa: ARG001
        message = web_server.make_agent_message(from_agent, content, to_agent, metadata=metadata)
        calls.append({"payload": payload, "message": message})
        job = web_server.Job(id="pseudo-delegate-job", kind="crawler", title=content, status="queued", summary="queued")
        job.result = {"plan": {"topic": content, "delivery_target": message.metadata.get("delivery_target")}}
        return {"answer": "我是 CrawlerAgent。采集任务已启动。", "agent": "crawler_agent", "job": web_server._job_to_dict(job)}

    original_context = web_server.build_agent_execution_context
    original_router = web_server.LlmAgentToolRouterService
    original_rag = web_server.RagRetrievalService
    original_evidence = web_server.EvidenceWorkflowService
    original_selector = web_server._selected_llm_client
    original_send = web_server._send_agent_message
    try:
        web_server.build_agent_execution_context = lambda *args, **kwargs: FakeRun()  # type: ignore[assignment]
        web_server.LlmAgentToolRouterService = FakeRouter  # type: ignore[assignment]
        web_server.RagRetrievalService = FakeRag  # type: ignore[assignment]
        web_server.EvidenceWorkflowService = FakeEvidenceWorkflow  # type: ignore[assignment]
        web_server._selected_llm_client = lambda *_args, **_kwargs: (fake_client, "fake")  # type: ignore[assignment]
        web_server._send_agent_message = fake_send  # type: ignore[assignment]
        result = web_server._chat_impl(
            SimpleNamespace(paths=SimpleNamespace(db_path=Path("test.sqlite"))),
            {"agent": "mcagent_rag", "question": question, "session_id": "pseudo-delegate"},
        )
    finally:
        web_server.build_agent_execution_context = original_context  # type: ignore[assignment]
        web_server.LlmAgentToolRouterService = original_router  # type: ignore[assignment]
        web_server.RagRetrievalService = original_rag  # type: ignore[assignment]
        web_server.EvidenceWorkflowService = original_evidence  # type: ignore[assignment]
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        web_server._send_agent_message = original_send  # type: ignore[assignment]

    assert_true("no_late_side_effect", not calls, str(calls))
    assert_true("pseudo_not_returned", "delegate_crawler(" not in str(result.get("answer") or ""), result.get("answer", ""))
    assert_true("no_job_returned", "job" not in result, str(result.get("job")))
    statuses = [(item.get("stage"), item.get("status")) for item in result.get("trace") or []]
    assert_true("protocol_trace", ("answer", "protocol_violation_detected") in statuses, str(statuses))
    assert_true("pseudo_removed_trace", ("answer", "pseudo_tool_text_removed") in statuses, str(statuses))


def test_unselected_pseudo_tool_text_is_removed_without_side_effect() -> None:
    tmp = tempfile.TemporaryDirectory()
    fake_client = SequencedClient(
        [
            json.dumps(
                {
                    "violation": True,
                    "tool": "delegate_crawler",
                    "action": "remove_pseudo_call",
                    "reason": "user did not request a crawler side effect",
                },
                ensure_ascii=False,
            )
        ]
    )
    original_selector = web_server._selected_llm_client
    try:
        web_server._selected_llm_client = lambda *_args, **_kwargs: (fake_client, "fake")  # type: ignore[assignment]
        review = web_server._final_answer_protocol_review(
            make_temp_config(Path(tmp.name)),
            agent="mcagent_rag",
            model="fake-model",
            original_question="请简单解释一下你能做什么。",
            answer="我可以回答问题。\n\ndelegate_crawler(\"偷偷采集\")",
            tool_decision={"tool": "answer", "reason": "simple explanation"},
            action_plan=[],
            planned_delegate=False,
        )
    finally:
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        tmp.cleanup()
    cleaned = web_server._strip_pseudo_tool_call_blocks("我可以回答问题。\n\ndelegate_crawler(\"偷偷采集\")")
    assert_equal("review_removes", review.get("action"), "remove_pseudo_call")
    assert_equal("pseudo_removed", cleaned, "我可以回答问题。")


def test_local_rag_route_review_cannot_add_unselected_delegate_side_effect() -> None:
    question = "现在乌托邦整合包你本地还缺哪些资料，列出来，然后让 Crawler 去补充。"
    selected = [
        SearchResult(
            rank=1,
            score=9.0,
            chunk_id=1,
            document_id=1,
            chunk_index=0,
            title="乌托邦探险之旅 本地资料盘点",
            source_path="D:/magic/MC_Agent/data/crawler_exports/utopia/local.md",
            url="https://example.test/utopia-local",
            text="本地已有版本和下载线索，但缺少玩法路线、任务线和毕业目标。",
            metadata={},
        )
    ]

    class FakeRun:
        original_question = "现在乌托邦整合包你本地还缺哪些资料，列出来，然后让 Crawler 去补充。"
        question = "现在乌托邦整合包你本地还缺哪些资料，列出来，然后让 Crawler 去补充。"
        agent = "mcagent_rag"
        model = ""
        temperature = 0.0
        max_tokens = 400
        is_streaming = False

        def __init__(self) -> None:
            self.config = SimpleNamespace(paths=SimpleNamespace(db_path=Path("test.sqlite")))
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
                tool_decision={"tool": "answer", "reason": "incorrect answer route"},
                route_intent="answer",
                action_plan=[],
                rag_focus="乌托邦整合包资料缺口",
                planned_workflow=False,
                planned_delegate=False,
            )

        def confirm_next_step(self, *args, **kwargs):
            return {"proceed": True, "tool": kwargs.get("proposed_tool"), "reason": "confirmed"}

    class FakeRag:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def prepare(self, *args, **kwargs):
            return SimpleNamespace(evidence_question="乌托邦整合包资料缺口", rough_k=8, final_k=6)

        def retrieve(self, *args, **kwargs):
            return SimpleNamespace(retrieval_plan=None, rough_results=selected, selected=selected)

    class FakeEvidenceWorkflow:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def select(self, *args, **kwargs):
            return SimpleNamespace(selected=selected, report=SimpleNamespace(verdict="ok", selected_count=1, reasons=[], to_dict=lambda: {"verdict": "ok"}))

    fake_client = SequencedClient(
        [
            json.dumps(
                {
                    "missing_side_effect": True,
                    "tool": "delegate_crawler",
                    "action": "execute_selected_tool",
                    "reason": "用户请求包含先列本地缺口再让 Crawler 补充，local_rag_search 只覆盖第一步。",
                    "collection_target": "补充乌托邦整合包玩法路线、任务线和毕业目标资料",
                    "delivery_target": "MCagent/RAG",
                },
                ensure_ascii=False,
            ),
            "本地已有版本和下载线索；缺少玩法路线、任务线和毕业目标。\n\n我现在就将以上缺口转达给 CrawlerAgent，并已通过计划工作流委托 CrawlerAgent 去补齐。",
            json.dumps({"handoff_brief": "MCagent sends local gaps to CrawlerAgent through AgentMessage.", "reason": "handoff"}, ensure_ascii=False),
        ]
    )
    calls: list[dict[str, Any]] = []

    def fake_send(config: AppConfig, payload: dict[str, Any], *, from_agent: str, content: str, to_agent: str, emit: Any | None = None, metadata: dict[str, Any] | None = None, **kwargs: Any):  # noqa: ARG001
        message = web_server.make_agent_message(from_agent, content, to_agent, metadata=metadata)
        calls.append({"payload": payload, "message": message})
        job = web_server.Job(id="natural-delegate-job", kind="crawler", title=content, status="queued", summary="queued")
        job.result = {"plan": {"topic": content, "delivery_target": message.metadata.get("delivery_target")}}
        return {"answer": "我是 CrawlerAgent。采集任务已启动。", "agent": "crawler_agent", "job": web_server._job_to_dict(job)}

    original_context = web_server.build_agent_execution_context
    original_router = web_server.LlmAgentToolRouterService
    original_rag = web_server.RagRetrievalService
    original_evidence = web_server.EvidenceWorkflowService
    original_selector = web_server._selected_llm_client
    original_send = web_server._send_agent_message
    try:
        web_server.build_agent_execution_context = lambda *args, **kwargs: FakeRun()  # type: ignore[assignment]
        web_server.LlmAgentToolRouterService = FakeRouter  # type: ignore[assignment]
        web_server.RagRetrievalService = FakeRag  # type: ignore[assignment]
        web_server.EvidenceWorkflowService = FakeEvidenceWorkflow  # type: ignore[assignment]
        web_server._selected_llm_client = lambda *_args, **_kwargs: (fake_client, "fake")  # type: ignore[assignment]
        web_server._send_agent_message = fake_send  # type: ignore[assignment]
        result = web_server._chat_impl(
            SimpleNamespace(paths=SimpleNamespace(db_path=Path("test.sqlite"))),
            {"agent": "mcagent_rag", "question": question, "session_id": "natural-delegate"},
        )
    finally:
        web_server.build_agent_execution_context = original_context  # type: ignore[assignment]
        web_server.LlmAgentToolRouterService = original_router  # type: ignore[assignment]
        web_server.RagRetrievalService = original_rag  # type: ignore[assignment]
        web_server.EvidenceWorkflowService = original_evidence  # type: ignore[assignment]
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        web_server._send_agent_message = original_send  # type: ignore[assignment]

    assert_true("no_unselected_delegate_message", not calls, str(calls))
    assert_true("no_job_returned", not result.get("job"), str(result.get("job")))
    statuses = [(item.get("stage"), item.get("status")) for item in result.get("trace") or []]
    assert_true("completeness_not_executed_trace", ("plan", "route_completeness_gap_not_executed") in statuses, str(statuses))


def test_conditional_delegate_suggestion_is_allowed_without_side_effect() -> None:
    tmp = tempfile.TemporaryDirectory()
    answer = "目前只能给出概要。如果需要，可以让 CrawlerAgent 继续补充公开资料。"
    review = web_server._final_answer_protocol_review(
        make_temp_config(Path(tmp.name)),
        agent="mcagent_rag",
        model="fake-model",
        original_question="简单介绍一下这个项目。",
        answer=answer,
        tool_decision={"tool": "answer", "reason": "simple explanation"},
        action_plan=[],
        planned_delegate=False,
        delegated_job_started=False,
    )
    tmp.cleanup()
    assert_equal("conditional_allowed", review.get("action"), "allow")
    assert_true("no_violation", not bool(review.get("violation")), str(review))


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


def test_utopia_journey_filter_keeps_real_chinese_pack_and_rejects_aoa_armor() -> None:
    wanted = SearchResult(
        rank=1,
        score=9.0,
        chunk_id=1,
        document_id=1,
        chunk_index=0,
        title="[UJ]乌托邦探险之旅 (Utopian Journey) - MC百科",
        source_path="D:/magic/MC_Agent/data/crawler_exports/fetch_url/utopian_journey.md",
        url="https://www.mcmod.cn/modpack/1337.html",
        text="3.6.6+1.20.1-fabric Fabric 整合包 核心玩法",
        metadata={},
    )
    armor = SearchResult(
        rank=2,
        score=8.0,
        chunk_id=2,
        document_id=2,
        chunk_index=0,
        title="乌托邦胸甲 (Utopian Chestplate) - [AoA2]虚无世界2",
        source_path="D:/magic/MC_Agent/data/crawler_exports/mcmod/aoa_utopian_chestplate.md",
        url="https://www.mcmod.cn/item/123.html",
        text="Utopian Chestplate armor item from Advent of Ascension.",
        metadata={},
    )
    filtered = web_server._filter_answer_evidence_by_required_terms(
        "Utopia Journey / 乌托邦探险之旅 modpack",
        [wanted, armor],
    )
    assert_equal("filtered_count", len(filtered), 1)
    assert_equal("kept_title", filtered[0].title, wanted.title)


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


def test_utopia_journey_alias_keeps_local_pack_evidence() -> None:
    source = SearchResult(
        rank=1,
        score=9.0,
        chunk_id=1,
        document_id=1,
        chunk_index=0,
        title="Utopia Journey / Utopian Journey modpack",
        source_path="D:/magic/MC_Agent/data/crawler_exports/web_discovery/utopia_journey.md",
        url="https://example.test/utopia-journey",
        text="Utopian Journey is a Minecraft 1.20.1 Fabric modpack with public download notes.",
        metadata={},
    )
    filtered = web_server._filter_answer_evidence_by_required_terms(
        "According to local stored sources, introduce Utopia Journey modpack.",
        [source],
    )
    assert_equal("utopia_journey_alias_kept", filtered, [source])


def test_entity_filter_recovers_matching_rough_candidate_when_selected_is_polluted() -> None:
    polluted = SearchResult(
        rank=1,
        score=9.0,
        chunk_id=1,
        document_id=1,
        chunk_index=0,
        title="Color item inventories on tooltips according to the container item's color.",
        source_path="D:/magic/MC_Agent/data/crawler_exports/modrinth_agent/mod_inventory.md",
        url="https://example.test/noise",
        text="A client-side tooltip mod unrelated to Utopian Journey.",
        metadata={},
    )
    target = SearchResult(
        rank=2,
        score=4.0,
        chunk_id=2,
        document_id=2,
        chunk_index=0,
        title="[UJ]乌托邦探险之旅 (Utopian Journey) - MC百科",
        source_path="D:/magic/MC_Agent/data/crawler_exports/fetch_url/utopian_journey.md",
        url="https://www.mcmod.cn/modpack/1337.html",
        text="乌托邦探险之旅 Utopian Journey Minecraft 1.20.1 Fabric 整合包，包含玩法、版本和下载说明。",
        metadata={},
    )
    recovered = web_server._filter_answer_evidence_with_recovery(
        "According to local stored sources, briefly introduce Utopia Journey modpack.",
        [polluted],
        [polluted, target],
        limit=4,
    )
    assert_equal("recovered_matching_entity", recovered, [target])


def test_version_install_fact_question_uses_agent_selected_local_rag_route() -> None:
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
        is_streaming = False

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
                tool_decision={"tool": "answer", "reason": "Agent chose local evidence search.", "rag_focus": question},
                route_intent="answer",
                action_plan=[],
                rag_focus=question,
                planned_workflow=False,
                planned_delegate=False,
            )

        def confirm_next_step(self, *args, **kwargs):
            return {"proceed": True, "tool": kwargs.get("proposed_tool"), "reason": "confirmed by Agent"}

    class FakeRag:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def prepare(self, *args, **kwargs):
            return SimpleNamespace(evidence_question=question, rough_k=8, final_k=6)

        def retrieve(self, *args, **kwargs):
            assert_true("planner_skipped_when_agent_supplied_rag_focus", kwargs.get("use_planner") is False)
            return SimpleNamespace(retrieval_plan=None, rough_results=[source], selected=[source])

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
    assert_true("rag_confirmed", ("retrieve", "next_step_confirmed") in statuses, str(statuses))
    assert_true("local_fact_trace", ("answer", "local_fact_answer") in statuses, str(statuses))


def test_agent_selected_local_corpus_inventory_route_executes_inventory_tool() -> None:
    class FakeRun:
        original_question = "本地都有哪些整合包和模组的资料 都简单介绍一下"
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

    def fail_rag(*_args, **_kwargs):
        raise AssertionError("Agent-selected inventory route should not run regular RAG retrieval")

    class FakeRouter:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def route(self, run, session_summary=None):
            return SimpleNamespace(
                tool_decision={"tool": "local_corpus_inventory", "reason": "Agent judged this as a local corpus coverage question."},
                route_intent="local_corpus_inventory",
                action_plan=[],
                rag_focus="",
                planned_workflow=False,
                planned_delegate=False,
            )

        def confirm_next_step(self, *args, **kwargs):
            return {"proceed": True, "tool": kwargs.get("proposed_tool"), "goal": kwargs.get("proposed_goal"), "reason": "confirmed by Agent"}

    def fake_inventory(_config, _question):
        return {
            "answer": "本地资料库目前有 2 篇已入库文档。\n\n整合包：乌托邦探险之旅。\n模组：Create。",
            "sources": [{"title": "乌托邦探险之旅", "document_id": 1}],
            "context": "inventory context",
            "agent": "mcagent_rag",
        }

    original_context = web_server.build_agent_execution_context
    original_router = web_server.LlmAgentToolRouterService
    original_rag = web_server.RagRetrievalService
    original_inventory = web_server._local_corpus_inventory_answer
    try:
        web_server.build_agent_execution_context = lambda *args, **kwargs: FakeRun()  # type: ignore[assignment]
        web_server.LlmAgentToolRouterService = FakeRouter  # type: ignore[assignment]
        web_server.RagRetrievalService = fail_rag  # type: ignore[assignment]
        web_server._local_corpus_inventory_answer = fake_inventory  # type: ignore[assignment]
        result = web_server._chat_impl(
            SimpleNamespace(paths=SimpleNamespace(db_path=Path("test.sqlite"))),
            {"agent": "mcagent_rag", "question": FakeRun.original_question},
        )
    finally:
        web_server.build_agent_execution_context = original_context  # type: ignore[assignment]
        web_server.LlmAgentToolRouterService = original_router  # type: ignore[assignment]
        web_server.RagRetrievalService = original_rag  # type: ignore[assignment]
        web_server._local_corpus_inventory_answer = original_inventory  # type: ignore[assignment]

    statuses = [item["status"] for item in result.get("trace") or []]
    assert_true("tool_selected", any(item.get("detail", {}).get("tool") == "local_corpus_inventory" for item in result.get("trace") or []), str(result.get("trace")))
    assert_true("inventory_confirmed", "inventory_next_step_confirmed" in statuses, str(statuses))
    assert_true("inventory_scanning_trace", "inventory_scanning" in statuses, str(statuses))
    assert_true("inventory_done_trace", "inventory_done" in statuses, str(statuses))
    assert_true("answer", "本地资料库目前有" in result.get("answer", ""), result.get("answer", ""))


def test_inventory_route_review_cannot_add_unselected_delegate_side_effect() -> None:
    question = "现在某个资料主题你本地还缺哪些资料，列出来，然后让 Crawler 去补充。"

    class FakeRun:
        original_question = "现在某个资料主题你本地还缺哪些资料，列出来，然后让 Crawler 去补充。"
        question = "现在某个资料主题你本地还缺哪些资料，列出来，然后让 Crawler 去补充。"
        agent = "mcagent_rag"
        model = ""
        temperature = 0.0
        max_tokens = 400
        is_streaming = False

        def __init__(self) -> None:
            self.config = SimpleNamespace(paths=SimpleNamespace(db_path=Path("test.sqlite")))
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
                tool_decision={"tool": "local_corpus_inventory", "reason": "inspect local coverage"},
                route_intent="local_corpus_inventory",
                action_plan=[],
                rag_focus="",
                planned_workflow=False,
                planned_delegate=False,
            )

        def confirm_next_step(self, *args, **kwargs):
            return {"proceed": True, "tool": kwargs.get("proposed_tool"), "goal": kwargs.get("proposed_goal"), "reason": "confirmed by Agent"}

    def fake_inventory(_config, _question):
        return {
            "answer": "本地资料库目前有 2 篇相关文档，但缺少完整玩法路线和来源页。",
            "sources": [{"title": "资料主题", "document_id": 1}],
            "context": "inventory context",
            "agent": "mcagent_rag",
        }

    fake_client = SequencedClient(
        [
            json.dumps(
                {
                    "missing_side_effect": True,
                    "tool": "delegate_crawler",
                    "action": "execute_selected_tool",
                    "reason": "The user requested local gap listing and then a real Crawler handoff.",
                    "collection_target": "请根据本地缺口补充该资料主题的完整玩法路线和来源页。",
                    "delivery_target": "MCagent/RAG",
                },
                ensure_ascii=False,
            ),
            json.dumps({"handoff_brief": "MCagent sends inventory gaps to CrawlerAgent through AgentMessage.", "reason": "handoff"}, ensure_ascii=False),
        ]
    )
    calls: list[Any] = []

    def fake_send(config: AppConfig, payload: dict[str, Any], *, from_agent: str, content: str, to_agent: str, emit: Any | None = None, metadata: dict[str, Any] | None = None, **kwargs: Any):  # noqa: ARG001
        message = web_server.make_agent_message(from_agent, content, to_agent, metadata=metadata)
        calls.append(message)
        job = web_server.Job(id="inventory-delegate-job", kind="crawler", title=content, status="queued", summary="queued")
        job.result = {"plan": {"topic": content, "delivery_target": message.metadata.get("delivery_target")}}
        return {"answer": "我是 CrawlerAgent。采集任务已启动。", "agent": "crawler_agent", "job": web_server._job_to_dict(job)}

    original_context = web_server.build_agent_execution_context
    original_router = web_server.LlmAgentToolRouterService
    original_inventory = web_server._local_corpus_inventory_answer
    original_selector = web_server._selected_llm_client
    original_send = web_server._send_agent_message
    try:
        web_server.build_agent_execution_context = lambda *args, **kwargs: FakeRun()  # type: ignore[assignment]
        web_server.LlmAgentToolRouterService = FakeRouter  # type: ignore[assignment]
        web_server._local_corpus_inventory_answer = fake_inventory  # type: ignore[assignment]
        web_server._selected_llm_client = lambda *_args, **_kwargs: (fake_client, "fake")  # type: ignore[assignment]
        web_server._send_agent_message = fake_send  # type: ignore[assignment]
        result = web_server._chat_impl(
            SimpleNamespace(paths=SimpleNamespace(db_path=Path("test.sqlite"))),
            {"agent": "mcagent_rag", "question": question, "session_id": "inventory-delegate"},
        )
    finally:
        web_server.build_agent_execution_context = original_context  # type: ignore[assignment]
        web_server.LlmAgentToolRouterService = original_router  # type: ignore[assignment]
        web_server._local_corpus_inventory_answer = original_inventory  # type: ignore[assignment]
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        web_server._send_agent_message = original_send  # type: ignore[assignment]

    assert_true("no_unselected_delegate_message", not calls, str(calls))
    assert_true("no_job_returned", not result.get("job"), str(result.get("job")))
    statuses = [(item.get("stage"), item.get("status")) for item in result.get("trace") or []]
    assert_true("completeness_trace", ("plan", "route_completeness_gap") in statuses, str(statuses))
    assert_true("not_executed_trace", ("plan", "inventory_missing_side_effect_not_executed") in statuses, str(statuses))


def test_local_corpus_inventory_is_not_keyword_forced_before_agent_choice() -> None:
    class FakeRun:
        original_question = "本地都有哪些整合包和模组的资料 都简单介绍一下"
        question = original_question
        agent = "mcagent_rag"
        model = ""
        temperature = 0.0
        max_tokens = 400
        is_streaming = False

        def __init__(self) -> None:
            self.config = SimpleNamespace(paths=SimpleNamespace(db_path=Path("test.sqlite")))
            self.trace = SimpleNamespace(steps=[])

        def add_trace(self, stage, status, detail=None):
            self.trace.steps.append({"stage": stage, "status": status, "detail": detail})
            return self.trace.steps[-1]

        def response(self, payload):
            payload["trace"] = self.trace.steps
            return payload

        def emit_delta(self, _text):
            pass

    class FakeRouter:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def route(self, run, session_summary=None):
            return SimpleNamespace(
                tool_decision={"tool": "direct_answer", "reason": "Agent chose to explain instead of inspecting local corpus."},
                route_intent="direct_answer",
                action_plan=[],
                rag_focus="",
                planned_workflow=False,
                planned_delegate=False,
            )

        def confirm_next_step(self, *args, **kwargs):
            raise AssertionError("direct answer route should not confirm inventory")

    def fail_inventory(*_args, **_kwargs):
        raise AssertionError("inventory tool must not run unless Agent selected local_corpus_inventory")

    def fake_direct_answer(*_args, **_kwargs):
        return "这是直接回答路径。"

    original_context = web_server.build_agent_execution_context
    original_router = web_server.LlmAgentToolRouterService
    original_inventory = web_server._local_corpus_inventory_answer
    original_direct = web_server._generate_direct_answer
    original_direct_stream = web_server._generate_direct_answer_stream
    try:
        web_server.build_agent_execution_context = lambda *args, **kwargs: FakeRun()  # type: ignore[assignment]
        web_server.LlmAgentToolRouterService = FakeRouter  # type: ignore[assignment]
        web_server._local_corpus_inventory_answer = fail_inventory  # type: ignore[assignment]
        web_server._generate_direct_answer = fake_direct_answer  # type: ignore[assignment]
        web_server._generate_direct_answer_stream = fake_direct_answer  # type: ignore[assignment]
        result = web_server._chat_impl(
            SimpleNamespace(paths=SimpleNamespace(db_path=Path("test.sqlite"))),
            {"agent": "mcagent_rag", "question": FakeRun.original_question},
        )
    finally:
        web_server.build_agent_execution_context = original_context  # type: ignore[assignment]
        web_server.LlmAgentToolRouterService = original_router  # type: ignore[assignment]
        web_server._local_corpus_inventory_answer = original_inventory  # type: ignore[assignment]
        web_server._generate_direct_answer = original_direct  # type: ignore[assignment]
        web_server._generate_direct_answer_stream = original_direct_stream  # type: ignore[assignment]

    statuses = [item["status"] for item in result.get("trace") or []]
    assert_true("no_inventory_scan", "inventory_scanning" not in statuses, str(statuses))
    assert_true("direct_answer", "这是直接回答路径。" in result.get("answer", ""), result.get("answer", ""))


def test_status_runs_only_after_agent_selects_status_tool() -> None:
    class FakeRun:
        original_question = "状态"
        question = original_question
        agent = "mcagent_rag"
        model = ""
        temperature = 0.0
        max_tokens = 400
        is_streaming = False

        def __init__(self) -> None:
            self.config = SimpleNamespace(paths=SimpleNamespace(db_path=Path("test.sqlite")))
            self.trace = SimpleNamespace(steps=[])

        def add_trace(self, stage, status, detail=None):
            self.trace.steps.append({"stage": stage, "status": status, "detail": detail})
            return self.trace.steps[-1]

        def response(self, payload):
            payload["trace"] = self.trace.steps
            return payload

        def emit_delta(self, _text):
            pass

    class FakeRouter:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def route(self, run, session_summary=None):
            return SimpleNamespace(
                tool_decision={"tool": "direct_answer", "reason": "Agent chose not to inspect runtime status."},
                route_intent="direct_answer",
                action_plan=[],
                rag_focus="",
                planned_workflow=False,
                planned_delegate=False,
            )

        def confirm_next_step(self, *args, **kwargs):
            raise AssertionError("direct answer route should not confirm status")

    original_context = web_server.build_agent_execution_context
    original_router = web_server.LlmAgentToolRouterService
    original_direct = web_server._generate_direct_answer
    original_direct_stream = web_server._generate_direct_answer_stream
    try:
        web_server.build_agent_execution_context = lambda *args, **kwargs: FakeRun()  # type: ignore[assignment]
        web_server.LlmAgentToolRouterService = FakeRouter  # type: ignore[assignment]
        web_server._generate_direct_answer = lambda *args, **kwargs: "直接回答状态问题。"  # type: ignore[assignment]
        web_server._generate_direct_answer_stream = lambda *args, **kwargs: "直接回答状态问题。"  # type: ignore[assignment]
        result = web_server._chat_impl(
            SimpleNamespace(paths=SimpleNamespace(db_path=Path("test.sqlite"))),
            {"agent": "mcagent_rag", "question": FakeRun.original_question},
        )
    finally:
        web_server.build_agent_execution_context = original_context  # type: ignore[assignment]
        web_server.LlmAgentToolRouterService = original_router  # type: ignore[assignment]
        web_server._generate_direct_answer = original_direct  # type: ignore[assignment]
        web_server._generate_direct_answer_stream = original_direct_stream  # type: ignore[assignment]

    statuses = [item["status"] for item in result.get("trace") or []]
    assert_true("no_status_tool", "next_step_confirmed" not in [item["status"] for item in result.get("trace") or [] if item.get("stage") == "status"], str(statuses))
    assert_true("direct_answer", "直接回答状态问题。" in result.get("answer", ""), result.get("answer", ""))


def test_agent_selected_crawler_audit_route_reads_audit_tool() -> None:
    class FakeRun:
        original_question = "刚才 Crawler 采集农夫乐事时哪些来源被拒绝？"
        question = original_question
        agent = "mcagent_rag"
        model = ""
        temperature = 0.0
        max_tokens = 400
        is_streaming = False

        def __init__(self) -> None:
            self.config = SimpleNamespace(paths=SimpleNamespace(db_path=Path("test.sqlite")))
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
                tool_decision={"tool": "crawler_audit", "reason": "Agent selected recent Crawler audit."},
                route_intent="crawler_audit",
                action_plan=[],
                rag_focus="",
                planned_workflow=False,
                planned_delegate=False,
            )

        def confirm_next_step(self, *args, **kwargs):
            return {"proceed": True, "tool": kwargs.get("proposed_tool"), "goal": kwargs.get("proposed_goal"), "reason": "confirmed"}

    original_context = web_server.build_agent_execution_context
    original_router = web_server.LlmAgentToolRouterService
    original_audit = web_server._recent_crawler_audit_answer
    try:
        web_server.build_agent_execution_context = lambda *args, **kwargs: FakeRun()  # type: ignore[assignment]
        web_server.LlmAgentToolRouterService = FakeRouter  # type: ignore[assignment]
        web_server._recent_crawler_audit_answer = lambda _question: {"answer": "Crawler 自审：拒绝 1 个来源。", "sources": [], "context": "", "agent": "mcagent_rag", "job": {"id": "job-audit"}}  # type: ignore[assignment]
        result = web_server._chat_impl(
            SimpleNamespace(paths=SimpleNamespace(db_path=Path("test.sqlite"))),
            {"agent": "mcagent_rag", "question": FakeRun.original_question},
        )
    finally:
        web_server.build_agent_execution_context = original_context  # type: ignore[assignment]
        web_server.LlmAgentToolRouterService = original_router  # type: ignore[assignment]
        web_server._recent_crawler_audit_answer = original_audit  # type: ignore[assignment]

    statuses = [(item.get("stage"), item.get("status")) for item in result.get("trace") or []]
    assert_true("audit_confirmed", ("audit", "next_step_confirmed") in statuses, str(statuses))
    assert_true("audit_answer", "Crawler 自审" in result.get("answer", ""), result.get("answer", ""))


def test_chat_router_does_not_force_agent_tool_choice_with_keyword_fast_paths() -> None:
    source = (ROOT / "mcagent" / "web_server.py").read_text(encoding="utf-8")
    start = source.index("def _chat_impl")
    end = source.index("\nclass MCagentHandler", start)
    body = source[start:end]
    forbidden = [
        "_should_start_explicit_mcagent_crawler_handoff_fast",
        "_should_force_mcagent_planned_delegate",
        "_should_force_crawler_mcagent_gap_workflow",
        "_should_use_deterministic_local_fact_rag_route",
        "explicit_mcagent_handoff_fast_path",
        "mcagent_delegate_workflow_corrected",
        "inter_agent_workflow_corrected",
        "Runtime-confirmed deterministic local fact route",
    ]
    for marker in forbidden:
        assert_true(f"no_forced_route_{marker}", marker not in body, marker)
    assert_true("side_effect_boundary_allowed", "side_effect_boundary_corrected" in body)


def test_general_answer_path_skips_local_fact_answer_for_modpack_overview() -> None:
    source = (ROOT / "mcagent" / "web_server.py").read_text(encoding="utf-8")
    assert_true(
        "overview_skips_local_fact_answer",
        "_needs_general_grounded_answer(original_question)"
        in source,
    )


def test_mcagent_context_tool_uses_message_bus_instead_of_internal_mcagent_shortcut() -> None:
    source = (ROOT / "mcagent" / "web_server.py").read_text(encoding="utf-8")
    start = source.index("def _run_mcagent_context_tool")
    end = source.index("\ndef _crawler_reusable_duplicate_evidence", start)
    body = source[start:end]
    assert_true("no_second_grounded_answer_call", "_generate_grounded_answer(" not in body)
    assert_true("uses_message_bus", "_send_agent_message(" in body)
    assert_true("no_fake_fast_context_trace", "structured_fast_context" not in body)


def test_mcagent_context_request_message_gets_light_reply_without_recursive_job() -> None:
    tmp = tempfile.TemporaryDirectory()

    class FakeRetriever:
        def __init__(self, _config):
            pass

        def search(self, query, top_k=16, session_summary=None):  # noqa: ANN001, ANN002, ARG002
            return [
                SearchResult(
                    rank=1,
                    score=9.0,
                    chunk_id=1,
                    document_id=1,
                    chunk_index=0,
                    title="乌托邦探险之旅本地资料",
                    source_path="D:/magic/MC_Agent/data/crawler_exports/demo.md",
                    url=None,
                    text="乌托邦探险之旅包含任务线、玩法路线、模组列表和配置说明。",
                )
            ]

    original_retriever = web_server.Retriever
    try:
        web_server.Retriever = FakeRetriever  # type: ignore[assignment]
        result = web_server._send_agent_message(
            make_temp_config(Path(tmp.name)),
            {"agent": "crawler_agent", "session_id": "mcagent-context-light-test"},
            from_agent="CrawlerAgent",
            content="请检查乌托邦整合包本地已有证据和缺口。",
            to_agent="MCagent",
            intent="mcagent_context_request",
            metadata={"tool": "mcagent_context", "collection_target": "乌托邦整合包"},
        )
    finally:
        web_server.Retriever = original_retriever  # type: ignore[assignment]
        tmp.cleanup()

    assert_true("answer", "本地已有证据候选" in result.get("answer", ""), result.get("answer", ""))
    assert_equal("no_job", result.get("job"), None)
    message = result.get("agent_message") if isinstance(result.get("agent_message"), dict) else {}
    assert_equal("from", message.get("from_agent"), "MCagent")
    assert_equal("to", message.get("to_agent"), "CrawlerAgent")
    assert_equal("transport", result.get("evidence", {}).get("transport"), "_send_agent_message")


def test_chat_runtime_timeout_returns_objective_blocker() -> None:
    tmp = tempfile.TemporaryDirectory()
    original_send = web_server._send_agent_message

    def slow_send(*_args, **_kwargs):  # noqa: ANN001
        time.sleep(0.25)
        return {"answer": "too late"}

    web_server._send_agent_message = slow_send  # type: ignore[assignment]
    started = time.time()
    try:
        result = web_server._chat(
            make_temp_config(Path(tmp.name)),
            {"agent": "mcagent_rag", "question": "让MCagent转达Crawler采集资料", "chat_timeout_seconds": 0.05},
        )
    finally:
        web_server._send_agent_message = original_send  # type: ignore[assignment]
        tmp.cleanup()

    assert_true("returns_before_slow_send_finishes", time.time() - started < 0.2)
    assert_true("timed_out", bool(result.get("timed_out")))
    assert_true("objective_blocker", "超过" in result.get("answer", "") and "任务列表" in result.get("answer", ""))
    diagnostics = result.get("diagnostics") or {}
    assert_equal("timeout_active_agent", diagnostics.get("active_agent"), "mcagent_rag")
    assert_equal("timeout_to_agent", diagnostics.get("to_agent"), "MCagent")
    assert_equal("timeout_runtime_seconds", diagnostics.get("chat_runtime_timeout_seconds"), 0.05)
    assert_true("timeout_profile_label_visible", bool(diagnostics.get("profile_label")), str(diagnostics))
    trace_detail = ((result.get("trace") or [{}])[0].get("detail") or {})
    assert_equal("trace_has_diagnostics", trace_detail.get("active_agent"), "mcagent_rag")


def test_delegate_handoff_brief_uses_bounded_llm_timeout() -> None:
    tmp = tempfile.TemporaryDirectory()
    original_selector = web_server._selected_llm_client
    seen_timeouts: list[int] = []

    class FakeClient:
        def chat(self, _messages, *, temperature=None, max_tokens=None):  # noqa: ANN001, ARG002
            return '{"handoff_brief":"brief","reason":"ok"}'

    def fake_selector(_config, _model, _temperature, **kwargs):  # noqa: ANN001
        seen_timeouts.append(int(kwargs.get("timeout_seconds") or 0))
        return FakeClient(), "fake"

    web_server._selected_llm_client = fake_selector  # type: ignore[assignment]
    try:
        brief, reason = web_server._build_delegate_handoff_brief(
            make_temp_config(Path(tmp.name)),
            model="fake-model",
            original_question="让 MCagent 转达 Crawler 采集资料",
            collection_target="采集资料",
            session_summary={},
            requested_by="user_via_mcagent",
            delivery_target="MCagent/RAG",
        )
    finally:
        web_server._selected_llm_client = original_selector  # type: ignore[assignment]
        tmp.cleanup()

    assert_equal("brief", brief, "brief")
    assert_equal("reason", reason, "ok")
    assert_true("bounded_handoff_timeout", bool(seen_timeouts) and 1 <= seen_timeouts[0] <= 30, str(seen_timeouts))


def test_mcagent_to_crawler_delegation_uses_message_bus_not_job_starter() -> None:
    source = (ROOT / "mcagent" / "web_server.py").read_text(encoding="utf-8")
    start = source.index("def _prepare_and_start_crawler_delegation")
    end = source.index("\ndef _delegation_handoff", start)
    body = source[start:end]
    assert_true("active_agent_gate", 'if active_agent == "crawler_agent"' in body)
    assert_true("non_crawler_sends_message", "_send_agent_message(" in body)
    assert_true("no_payload_agent_start_gate", 'delegate_payload.get("agent")' not in body)
    assert_true("handoff_elapsed_trace", '"elapsed_ms": round((time.time() - prepare_started) * 1000)' in body)
    assert_true("message_elapsed_trace", '"elapsed_ms": round((time.time() - message_started) * 1000)' in body)


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


def test_zero_byte_artifact_is_visible_but_not_accepted_for_ingest() -> None:
    with tempfile.TemporaryDirectory(prefix="mcagent-empty-artifact-") as tmp:
        export_dir = Path(tmp)
        empty = export_dir / "empty.md"
        empty.write_text("", encoding="utf-8")
        (export_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "provider": "save_artifact",
                    "records": [
                        {
                            "title": "Crawler summary",
                            "path": str(empty),
                            "bytes": 0,
                        }
                    ],
                    "skipped": [],
                    "errors": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        stats = web_server._crawler_manifest_stats(str(export_dir))
        assert_equal("records", stats["records"], 1)
        assert_equal("usable_records", stats["usable_records"], 0)
        assert_equal("empty_records", stats["empty_records"], 1)
        result = {
            "source": "save_artifact",
            "query": "Farmer's Delight summary",
            "returncode": 0,
            "export_dir": str(export_dir),
            "manifest_stats": stats,
            "topic_validation": {"matched": True, "matched_indexes": [0], "reason": "direct"},
        }
        roots = web_server._crawler_accepted_ingest_roots(result)
        assert_equal("empty_not_ingested", roots, [])
        summary = web_server._crawler_result_summary([result], {"topic": "Farmer's Delight"})
        assert_equal("no_useful_records", summary["useful_records"], [])


def test_structured_manifest_records_count_as_usable_objective_content() -> None:
    with tempfile.TemporaryDirectory(prefix="mcagent-structured-manifest-") as tmp:
        export_dir = Path(tmp)
        (export_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "provider": "browser_collect",
                    "status": "ok",
                    "records": [
                        {
                            "name": "Packard 255 G2",
                            "price": "416.99",
                            "url": "https://webscraper.io/test-sites/e-commerce/static/product/31",
                            "source": "https://webscraper.io/test-sites/e-commerce/static/computers/laptops",
                        }
                    ],
                    "skipped": [],
                    "errors": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        stats = web_server._crawler_manifest_stats(str(export_dir))
        assert_equal("records", stats["records"], 1)
        assert_equal("usable_records", stats["usable_records"], 1)
        assert_equal("empty_records", stats["empty_records"], 0)
        assert_true("record_bytes", int(stats["record_bytes"]) > 0)


def test_manifest_preview_filters_encoding_damaged_fields() -> None:
    with tempfile.TemporaryDirectory(prefix="mcagent-manifest-preview-") as tmp:
        export_dir = Path(tmp)
        damaged = "".join(chr(code) for code in (0x00E9, 0x0097, 0x00AE, 0x00E4, 0x00B8, 0x008B))
        (export_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "provider": "web_discovery",
                    "status": "ok",
                    "records": [],
                    "candidates": [
                        {
                            "title": "乌托邦整合包资料页",
                            "url": "https://example.test/utopia-pack",
                            "snippet": damaged,
                        }
                    ],
                    "skipped": [],
                    "errors": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        stats = web_server._crawler_manifest_stats(str(export_dir))
        preview = stats["candidate_preview"][0]
        assert_equal("preview_title_kept", preview["title"], "乌托邦整合包资料页")
        assert_equal("preview_url_kept", preview["url"], "https://example.test/utopia-pack")
        assert_true("damaged_snippet_removed", "snippet" not in preview, str(preview))


def test_structured_manifest_fields_are_visible_to_crawler_review() -> None:
    record = {
        "name": "Packard 255 G2",
        "price": "416.99",
        "url": "https://webscraper.io/test-sites/e-commerce/static/product/31",
        "source": "https://webscraper.io/test-sites/e-commerce/static/computers/laptops",
    }
    assert_true("structured_has_content", web_server._crawler_record_has_content(record))
    text = web_server._record_text_for_validation(record)
    assert_true("name_visible", "Packard 255 G2" in text, text)
    assert_true("price_visible", "416.99" in text, text)
    assert_true("source_visible", "computers/laptops" in text, text)


def test_job_readable_refreshes_legacy_manifest_stats() -> None:
    with tempfile.TemporaryDirectory(prefix="mcagent-legacy-empty-artifact-") as tmp:
        export_dir = Path(tmp)
        empty = export_dir / "empty.md"
        empty.write_text("", encoding="utf-8")
        (export_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "provider": "save_artifact",
                    "records": [{"title": "Empty saved summary", "path": str(empty), "bytes": 0}],
                    "skipped": [],
                    "errors": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        job = {
            "title": "Crawler 采集",
            "status": "succeeded",
            "result": {
                "tasks": [
                    {
                        "source": "save_artifact",
                        "query": "Farmer's Delight summary",
                        "returncode": 0,
                        "export_dir": str(export_dir),
                        "manifest_stats": {"records": 1},
                        "topic_validation": {"matched": True, "matched_indexes": [0], "reason": "direct"},
                    }
                ]
            },
        }
        readable = web_server._job_readable_summary(job)
        assert_equal("refreshed_status", readable["latest_observation"]["status"], "records_pending_review")
        assert_equal("accepted_count", readable["self_audit"]["counts"]["accepted"], 0)
        assert_equal("pending_count", readable["self_audit"]["counts"]["pending_review"], 1)
        assert_equal("useful_outputs", readable["useful_outputs"], [])


def test_light_job_plan_preserves_model_prior_boundary() -> None:
    light = web_server._light_job_plan(
        {
            "topic": "Farmer's Delight",
            "model_prior": {
                "target": "Farmer's Delight",
                "aliases": ["Farmer's Delight", "农夫乐事"],
                "likely_source_graph": ["wiki", "Modrinth project/files"],
                "search_leads": ["Farmer's Delight guide", "Farmer's Delight Modrinth"],
                "verification_questions": ["Which source verifies progression?"],
                "evidence_status": "hypothesis_only",
                "allowed_use": "planning_only",
                "forbidden_use": "Do not cite, ingest, or mark as accepted evidence until objective tools verify it.",
            },
        }
    )
    assert_equal("prior_status", light["model_prior"]["evidence_status"], "hypothesis_only")
    assert_equal("prior_allowed_use", light["model_prior"]["allowed_use"], "planning_only")
    assert_true("prior_leads_visible", "Farmer's Delight guide" in light["model_prior"]["search_leads"])


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


def test_archive_url_helper_and_fetch_url_boundary() -> None:
    assert_equal("mrpack_url", web_server._looks_like_archive_url("https://cdn.example.test/packs/demo.mrpack"), True)
    assert_equal("zip_url", web_server._looks_like_archive_url("fetch https://cdn.example.test/packs/demo.zip please"), True)
    assert_equal("page_url", web_server._looks_like_archive_url("https://example.test/page.html"), False)
    command = web_server._round_command("fetch_url", {"query": "https://cdn.example.test/packs/demo.mrpack"})
    assert_true("still_objective_fetch_tool", "fetch_url_seed.py" in " ".join(command))


def test_modpack_download_direct_archive_url_is_range_probed() -> None:
    from unittest.mock import patch  # noqa: PLC0415

    from scripts import fetch_modpack_archive_seed as seed  # noqa: PLC0415

    def fake_probe(candidate: dict, user_agent: str, timeout: int = 45):  # noqa: ARG001
        candidate.update({"probe_status": 206, "probe_magic": "504b0304", "size": 1024})
        return candidate

    with patch.object(seed, "candidate_with_probe", side_effect=fake_probe):
        candidate = seed.direct_archive_candidate("https://cdn.example.test/packs/demo.mrpack", user_agent="unit-test")
    assert_true("candidate", isinstance(candidate, dict))
    assert_equal("source", candidate["source"], "direct_url")
    assert_equal("downloadable", seed.archive_candidate_is_downloadable(candidate), True)


def test_modpack_download_modrinth_search_uses_clean_alias_variant() -> None:
    from unittest.mock import patch  # noqa: PLC0415

    from scripts import fetch_modpack_archive_seed as seed  # noqa: PLC0415

    calls: list[str] = []

    def fake_request_text(url: str, user_agent: str, timeout: int = 30):  # noqa: ARG001
        calls.append(url)
        if "/search?" in url and "Craftoria%20modpack%20.mrpack%20.zip" in url:
            return '{"hits":[]}', "application/json", 200
        if "/search?" in url and "Craftoria" in url:
            return '{"hits":[{"title":"Craftoria","slug":"craftoria","project_id":"project-1"}]}', "application/json", 200
        if "/project/project-1/version" in url:
            return (
                '[{"version_number":"1.0.0","files":[{"filename":"Craftoria.mrpack","url":"https://cdn.modrinth.com/data/project/versions/file/Craftoria.mrpack","size":42,"primary":true}]}]',
                "application/json",
                200,
            )
        raise AssertionError(url)

    with patch.object(seed, "request_text", side_effect=fake_request_text):
        candidates, errors = seed.modrinth_archive_candidates("Craftoria modpack .mrpack .zip", user_agent="unit-test", limit=8)
    assert_equal("errors", errors, [])
    assert_equal("candidate_count", len(candidates), 1)
    assert_equal("discovery_query", candidates[0]["discovery_query"], "Craftoria")
    assert_true("clean_alias_called", any("query=Craftoria" in url for url in calls))


def test_modpack_download_skips_download_when_candidate_name_mismatches_target() -> None:
    from scripts import fetch_modpack_archive_seed as seed  # noqa: PLC0415

    mismatch = {
        "project_title": "Craft Royale",
        "project_slug": "craftroyale",
        "filename": "Craft Royale.mrpack",
        "url": "https://cdn.modrinth.com/data/demo/Craft%20Royale.mrpack",
        "archive_magic": "zip",
    }
    exact = {
        "project_title": "Craftoria",
        "project_slug": "craftoria",
        "filename": "Craftoria.mrpack",
        "url": "https://cdn.modrinth.com/data/demo/Craftoria.mrpack",
        "archive_magic": "zip",
    }
    assert_equal("mismatch", seed.archive_candidate_matches_target(mismatch, "Craftoria modpack .mrpack .zip"), False)
    assert_equal("exact", seed.archive_candidate_matches_target(exact, "Craftoria modpack .mrpack .zip"), True)


def test_modpack_download_discovers_curseforge_mediafilez_candidate() -> None:
    from unittest.mock import patch  # noqa: PLC0415

    from scripts import fetch_modpack_archive_seed as seed  # noqa: PLC0415

    def fake_request_text(url: str, user_agent: str, timeout: int = 30):  # noqa: ARG001
        if "api.cfwidget.com/minecraft/modpacks/craftoria" in url:
            return (
                '{"id":1039252,"title":"Craftoria","summary":"Demo","download":{"id":8127261,"url":"https://www.curseforge.com/minecraft/modpacks/craftoria/files/8127261","display":"Craftoria - 1.31.0","name":"Craftoria-1.31.0.zip","type":"release","version":"1.21.1","filesize":62463455,"versions":["1.21.1","NeoForge"]},"files":[]}',
                "application/json",
                200,
            )
        raise AssertionError(url)

    def fake_probe(candidate: dict, user_agent: str, timeout: int = 45):  # noqa: ARG001
        candidate.update({"probe_status": 206, "probe_magic": "504b0304", "probe_content_range": "bytes 0-4095/62463455"})
        return candidate

    with patch.object(seed, "request_text", side_effect=fake_request_text), patch.object(seed, "candidate_with_probe", side_effect=fake_probe):
        candidates, pages, errors = seed.curseforge_archive_candidates("Craftoria modpack .mrpack .zip", user_agent="unit-test", limit=3)
    assert_equal("errors", errors, [])
    assert_equal("pages", len(pages), 1)
    assert_equal("candidates", len(candidates), 1)
    assert_true("mediafilez_url", candidates[0]["url"].startswith("https://mediafilez.forgecdn.net/files/8127/261/Craftoria-1.31.0.zip"))
    assert_equal("matches_target", seed.archive_candidate_matches_target(candidates[0], "Craftoria modpack .mrpack .zip"), True)


def test_modpack_download_search_queries_use_readable_chinese_terms() -> None:
    from scripts.fetch_modpack_archive_seed import archive_discovery_search_queries, inferred_official_site_urls, official_site_search_queries  # noqa: PLC0415

    queries = archive_discovery_search_queries("乌托邦探险之旅")
    joined = "\n".join(queries)
    assert_true("official_site_query", "乌托邦探险之旅 官网" in queries)
    assert_true("client_query", "乌托邦探险之旅 客户端" in queries)
    assert_true("guide_query", "乌托邦探险之旅 下载 指南" in queries)
    assert_true("no_placeholder_queries", "??" not in joined)
    official_queries = official_site_search_queries("乌托邦探险之旅")
    assert_true("minepixel_official_query", "乌托邦探险之旅 MinePixel" in official_queries)
    assert_true("server_official_query", "乌托邦探险之旅 服务器 官网" in official_queries)
    assert_true("minebbs_source_query", "site:minebbs.com 乌托邦探险之旅" in official_queries)
    assert_true("inferred_official_domain", "https://www.minepixel.top/" in inferred_official_site_urls("乌托邦探险之旅"))


def test_encoding_damage_guard_catches_mojibake_without_blocking_valid_chinese() -> None:
    good = {"question": "乌托邦探险之旅 整合包 .mrpack .zip"}
    mojibake = "".join(chr(code) for code in (0x6D94, 0x58AD, 0x95AD, 0x93C1, 0x9356, 0x934F))
    bad = {"question": f"{mojibake}.mrpack .zip"}
    assert_equal("good_chinese", web_server._has_likely_encoding_damage(good), False)
    assert_equal("bad_mojibake", web_server._has_likely_encoding_damage(bad), True)


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


def test_modpack_download_has_bounded_probe_timeout() -> None:
    assert_equal("modpack_download_timeout", web_server._command_timeout("modpack_download"), 420)


def test_mcmod_search_has_bounded_task_budget() -> None:
    command = web_server._round_command("mcmod", {"query": "乌托邦探险之旅 模组列表", "search_limit": 50})
    assert_equal("mcmod_timeout", web_server._command_timeout("mcmod"), 90)
    assert_equal("mcmod_limit", command[command.index("--limit") + 1], "6")


def test_public_discovery_tools_have_bounded_task_budget() -> None:
    web_command = web_server._round_command("web_discovery", {"query": "乌托邦探险之旅 玩法", "search_limit": 50, "max_urls": 50})
    playwright_command = web_server._round_command("playwright", {"query": "乌托邦探险之旅 玩法", "search_limit": 50, "max_urls": 50})
    assert_equal("web_discovery_timeout", web_server._command_timeout("web_discovery"), 120)
    assert_equal("playwright_timeout", web_server._command_timeout("playwright"), 150)
    assert_equal("web_results", web_command[web_command.index("--max-results") + 1], "4")
    assert_equal("web_pages", web_command[web_command.index("--max-pages") + 1], "3")
    assert_equal("web_variants", web_command[web_command.index("--max-variants") + 1], "3")
    assert_equal("web_request_timeout", web_command[web_command.index("--request-timeout") + 1], "8")
    assert_equal("web_budget", web_command[web_command.index("--budget-seconds") + 1], "60")
    assert_true("playwright_mcp_style_backend", "playwright_mcp_seed.py" in " ".join(playwright_command), detail=str(playwright_command))
    assert_equal("playwright_results", playwright_command[playwright_command.index("--max-results") + 1], "3")
    assert_equal("playwright_pages", playwright_command[playwright_command.index("--max-pages") + 1], "2")
    assert_equal("playwright_snapshot_depth", playwright_command[playwright_command.index("--snapshot-depth") + 1], "3")


def test_crawler_reflection_timeout_continues_with_pending_task() -> None:
    original_reflect = web_server.reflect_crawler_progress

    def slow_reflect(*_args, **_kwargs):
        time.sleep(2)
        return {"action": "execute_pending"}

    web_server.reflect_crawler_progress = slow_reflect  # type: ignore[assignment]
    try:
        started = time.monotonic()
        decision = web_server._reflect_crawler_progress_with_timeout(
            "collect target",
            {"topic": "target"},
            [{"source": "web_discovery", "empty_result": True}],
            [{"source": "playwright", "query": "target"}],
            session_summary={},
            max_new_tasks=2,
            timeout_seconds=1,
        )
    finally:
        web_server.reflect_crawler_progress = original_reflect  # type: ignore[assignment]
    elapsed = time.monotonic() - started
    assert_true("timeout_returned_before_slow_reflect_finished", elapsed < 1.5, detail=f"elapsed={elapsed:.3f}s")
    assert_equal("timeout_action", decision.get("action"), "execute_pending")
    assert_equal("timeout_selected", decision.get("selected_index"), 0)
    assert_equal("timeout_planner", decision.get("planner"), "runtime_reflection_timeout")
    assert_true("timeout_issue", "reflection_timeout_continued_with_pending_task" in decision.get("contract", {}).get("issues", []))


def test_crawler_reflection_timeout_finishes_after_repeated_low_yield() -> None:
    original_reflect = web_server.reflect_crawler_progress

    def slow_reflect(*_args, **_kwargs):
        time.sleep(2)
        return {"action": "execute_pending"}

    web_server.reflect_crawler_progress = slow_reflect  # type: ignore[assignment]
    try:
        started = time.monotonic()
        decision = web_server._reflect_crawler_progress_with_timeout(
            "collect target",
            {"topic": "target"},
            [
                {"source": "web_discovery", "empty_result": True},
                {"source": "playwright", "empty_result": True},
            ],
            [],
            session_summary={},
            max_new_tasks=2,
            timeout_seconds=1,
        )
    finally:
        web_server.reflect_crawler_progress = original_reflect  # type: ignore[assignment]
    elapsed = time.monotonic() - started
    assert_true("timeout_returned_before_slow_reflect_finished", elapsed < 1.5, detail=f"elapsed={elapsed:.3f}s")
    assert_equal("timeout_action", decision.get("action"), "finish")
    assert_equal("timeout_planner", decision.get("planner"), "runtime_reflection_timeout")
    assert_true("timeout_issue", "reflection_timeout_finished_after_low_yield" in decision.get("contract", {}).get("issues", []))


def test_crawler_reflection_timeout_continues_low_yield_when_pending_exists() -> None:
    original_reflect = web_server.reflect_crawler_progress

    def slow_reflect(*_args, **_kwargs):
        time.sleep(2)
        return {"action": "finish"}

    web_server.reflect_crawler_progress = slow_reflect  # type: ignore[assignment]
    try:
        decision = web_server._reflect_crawler_progress_with_timeout(
            "collect target",
            {"topic": "target"},
            [
                {"source": "web_discovery", "empty_result": True},
                {"source": "playwright", "empty_result": True},
            ],
            [{"source": "web_discovery", "query": "target guide"}],
            session_summary={},
            max_new_tasks=2,
            timeout_seconds=1,
        )
    finally:
        web_server.reflect_crawler_progress = original_reflect  # type: ignore[assignment]
    assert_equal("timeout_action", decision.get("action"), "execute_pending")
    issues = decision.get("contract", {}).get("issues", [])
    assert_true("continued_issue", "reflection_timeout_continued_with_pending_task" in issues, issues)
    assert_true("low_yield_issue_visible", "reflection_timeout_low_yield_but_pending_task_available" in issues, issues)


def test_modpack_download_defaults_to_probe_only_command() -> None:
    command = web_server._round_command("modpack_download", {"query": "Utopian Journey", "search_limit": 3})
    assert_true("probe_only", "--no-download" in command)
    assert_true("quick_probe", "--quick-probe" in command)
    assert_equal("limit", command[command.index("--limit") + 1], "3")


def test_modpack_download_allows_explicit_full_download_command() -> None:
    command = web_server._round_command("modpack_download", {"query": "Utopian Journey", "download": True})
    assert_true("full_download", "--no-download" not in command)
    assert_true("no_quick_probe_for_full_download", "--quick-probe" not in command)


def test_modpack_archive_lookup_skips_corrupt_zip() -> None:
    import tempfile  # noqa: PLC0415

    original_root = web_server.PROJECT_ROOT
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        archive = root / "data" / "manual_research" / "modpack_archives" / "demo" / "pack_archive" / "demo.zip"
        archive.parent.mkdir(parents=True)
        archive.write_bytes(b"partial download")
        try:
            web_server.PROJECT_ROOT = root  # type: ignore[misc]
            assert_equal("lookup", web_server._modpack_archive_for_query("demo"), "")
        finally:
            web_server.PROJECT_ROOT = original_root  # type: ignore[misc]


def test_record_validation_skips_binary_archive_body() -> None:
    import tempfile  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "demo.zip"
        archive.write_bytes(b"PK\x03\x04" + b"x" * 1024)
        text = web_server._record_text_for_validation({"title": "Downloaded archive", "path": str(archive)})
    assert_true("keeps_title", "Downloaded archive" in text)
    assert_true("skips_binary", "PK" not in text)


def test_modpack_download_follows_public_release_seed() -> None:
    from unittest.mock import patch  # noqa: PLC0415

    from scripts import fetch_modpack_archive_seed as seed  # noqa: PLC0415

    def fake_request_text(url: str, user_agent: str, timeout: int = 30):  # noqa: ARG001
        if url == "https://example.test/dw/get.txt":
            return ("pack:\nhttps://cnb.cool/example/demo/-/releases/download/test/demo.zip", "text/plain", 200)
        raise RuntimeError("not found")

    def fake_probe(candidate: dict, user_agent: str, timeout: int = 45):  # noqa: ARG001
        candidate.update({"probe_status": 206, "probe_content_type": "application/zip", "archive_magic": "zip", "size": 1234})
        return candidate

    with patch.object(seed, "request_text", side_effect=fake_request_text), patch.object(seed, "candidate_with_probe", side_effect=fake_probe):
        candidates, pages, errors = seed.public_release_candidates(
            "Any community modpack",
            user_agent="unit-test",
            limit=5,
            discovery_pages=[{"url": "https://example.test/"}],
        )
    assert_true("only_seed_fetch_errors", all(item.get("stage") == "release_seed_fetch" for item in errors))
    assert_equal("pages", len(pages), 1)
    assert_equal("candidate_count", len(candidates), 1)
    assert_equal("candidate_source", candidates[0]["source"], "public_release_seed")
    assert_equal("candidate_url", candidates[0]["url"], "https://cnb.cool/example/demo/-/releases/download/test/demo.zip")
    assert_equal("probe_status", candidates[0]["probe_status"], 206)


def test_modpack_download_prioritizes_verified_public_release_candidate() -> None:
    from scripts import fetch_modpack_archive_seed as seed  # noqa: PLC0415

    unverified = {
        "source": "bbsmc",
        "project_title": "乌托邦探险之旅",
        "filename": "wtbtxzl3.2fix.zip",
        "url": "https://example.test/wtbtxzl3.2fix.zip",
    }
    verified = {
        "source": "public_release_seed",
        "page_url": "https://www.minepixel.top/dw/get.txt",
        "filename": "MinePIxelWuTuoBang3.5.1Fix.zip",
        "url": "https://cnb.cool/minepixel.top/test/-/releases/download/test/MinePIxelWuTuoBang3.5.1Fix.zip",
        "probe_status": 206,
        "probe_content_type": "application/zip",
        "probe_content_range": "bytes 0-4095/2173283032",
        "probe_magic": "504b030414000000",
        "archive_magic": "zip",
    }
    ranked = seed.prioritize_archive_candidates([unverified, verified], "乌托邦探险之旅 / Utopian Journey 整合包 .mrpack .zip")
    assert_equal("verified_first", ranked[0]["url"], verified["url"])
    assert_equal("unverified_not_downloadable", seed.archive_candidate_is_downloadable(unverified), False)
    assert_equal("verified_downloadable", seed.archive_candidate_is_downloadable(verified), True)


def test_modpack_download_filters_generic_google_archives() -> None:
    from scripts import fetch_modpack_archive_seed as seed  # noqa: PLC0415

    assert_equal(
        "google_interland_not_public_page",
        seed.usable_public_page_url("https://storage.googleapis.com/gweb-interland.appspot.com/th-all/interland/files/Google_Interland_GameSuccess.zip"),
        False,
    )


def test_modpack_download_writes_download_evidence_markdown() -> None:
    import tempfile  # noqa: PLC0415

    from scripts import fetch_modpack_archive_seed as seed  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as tmp:
        paths = seed.write_download_evidence(
            Path(tmp),
            "乌托邦探险之旅",
            [
                {
                    "filename": "MinePIxelWuTuoBang3.5.1Fix.zip",
                    "page_url": "https://www.minepixel.top/dw/get.txt",
                    "url": "https://cnb.cool/minepixel.top/test/-/releases/download/test/MinePIxelWuTuoBang3.5.1Fix.zip",
                    "probe_status": 206,
                    "probe_content_type": "application/zip",
                    "probe_content_range": "bytes 0-4095/2173283032",
                    "probe_magic": "504b030414000000",
                    "archive_magic": "zip",
                    "bytes": 2173283032,
                    "sha256": "5479e238489b9d2ec232de43d99797a76bedc277d6814a60f8bb2313f1fe9cce",
                    "path": "D:/packs/MinePIxelWuTuoBang3.5.1Fix.zip",
                    "validation": {"entries": 7891, "has_minecraft_version_instance": True, "instance_root": ".minecraft/versions/demo/"},
                }
            ],
        )
        text = paths[0].read_text(encoding="utf-8")
    assert_true("evidence_url", "https://www.minepixel.top/dw/get.txt" in text)
    assert_true("evidence_sha", "5479e238489b9d2ec232de43d99797a76bedc277d6814a60f8bb2313f1fe9cce" in text)
    assert_true("evidence_bytes", "2173283032" in text)


def test_modpack_download_decodes_mcmod_external_redirect_links() -> None:
    from scripts import fetch_modpack_archive_seed as seed  # noqa: PLC0415

    links = seed.extract_links(
        '<a href="//link.mcmod.cn/target/aHR0cHM6Ly93d3cueHllYmJzLmNvbS9yZXMtaWQvV1RCMg==">XyeBBS</a>',
        "https://www.mcmod.cn/modpack/1337.html",
    )
    assert_true("decoded_external_link", "https://www.xyebbs.com/res-id/WTB2" in links)


def test_download_archive_reuses_valid_existing_zip() -> None:
    import tempfile  # noqa: PLC0415
    import zipfile  # noqa: PLC0415

    from scripts import fetch_modpack_archive_seed as seed  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as tmp:
        archive_dir = Path(tmp)
        existing = archive_dir / "demo.zip"
        with zipfile.ZipFile(existing, "w") as zipped:
            zipped.writestr("manifest.json", "{}")
            zipped.writestr("modlist.html", "<html></html>")
        existing_size = existing.stat().st_size
        saved = seed.download_archive({"url": "https://example.invalid/demo.zip", "filename": "demo.zip"}, archive_dir, "unit-test", max_bytes=1024)
    assert_equal("reused_existing", saved["reused_existing"], True)
    assert_equal("bytes", saved["bytes"], existing_size)


def test_ranged_download_resumes_existing_part_file() -> None:
    import tempfile  # noqa: PLC0415
    from unittest.mock import patch  # noqa: PLC0415

    from scripts import fetch_modpack_archive_seed as seed  # noqa: PLC0415

    class FakeResponse:
        def __init__(self, body: bytes) -> None:
            self.body = body
            self.status = 206

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
            return None

        def read(self, size: int = -1) -> bytes:
            if not self.body:
                return b""
            if size < 0:
                size = len(self.body)
            chunk = self.body[:size]
            self.body = self.body[size:]
            return chunk

    seen_ranges: list[str] = []

    def fake_urlopen(request, timeout: int = 0):  # noqa: ANN001, ARG001
        range_header = request.headers.get("Range")
        seen_ranges.append(range_header)
        start, end = [int(item) for item in range_header.removeprefix("bytes=").split("-")]
        return FakeResponse(b"x" * (end - start + 1))

    with tempfile.TemporaryDirectory() as tmp:
        part_path = Path(tmp) / "demo.zip.part"
        part_path.write_bytes(b"a" * 5)
        with patch.object(seed.urllib.request, "urlopen", side_effect=fake_urlopen):
            seed.ranged_download("https://example.test/demo.zip", part_path, user_agent="unit-test", total_size=11, timeout=5)
        assert_equal("size", part_path.stat().st_size, 11)
        assert_equal("first_range", seen_ranges[0], "bytes=5-10")


def test_modpack_internal_detects_minecraft_instance_layout() -> None:
    import tempfile  # noqa: PLC0415
    import zipfile  # noqa: PLC0415

    from scripts.extract_modpack_internals import extract  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        archive = root / "demo.zip"
        instance = ".minecraft/versions/DemoPack/"
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr(".minecraft/versions/", "")
            zipped.writestr(instance, "")
            zipped.writestr(instance + "DemoPack.json", json.dumps({"id": "DemoPack"}, ensure_ascii=False))
            zipped.writestr(instance + "mods/example.jar", b"jar")
            zipped.writestr(instance + "config/ftbquests/quests/chapters/intro.snbt", 'title: "Intro"\ndescription: ["Start"]\nitem: "minecraft:apple"')
            zipped.writestr(instance + "kubejs/server_scripts/recipes.js", 'event.shaped("minecraft:stick", ["A"], {A: "minecraft:apple"})')
        data = extract(archive, root / "exports", root / "manual")
        assert_equal("layout_kind", data["layout"]["kind"], "minecraft_instance")
        assert_equal("quest_files", data["stats"]["quest_chapter_files"], 1)
        assert_equal("kubejs_scripts", data["stats"]["kubejs_scripts"], 1)
        route_record = next(record for record in data["records"] if str(record["path"]).endswith("DemoPack_gameplay_route_index.md"))
        route_text = Path(str(route_record["path"])).read_text(encoding="utf-8")
        assert_true("route_terms", "retrieval_terms" in route_text and "新手" in route_text)
        assert_true("route_chapter", "Intro" in route_text)


def test_modpack_internal_uses_curseforge_overrides_root() -> None:
    import tempfile  # noqa: PLC0415
    import zipfile  # noqa: PLC0415

    from scripts.extract_modpack_internals import extract  # noqa: PLC0415

    manifest = {
        "manifestType": "minecraftModpack",
        "name": "Demo CurseForge Pack",
        "version": "1.0.0",
        "minecraft": {"version": "1.21.1", "modLoaders": [{"id": "neoforge-21.1.1", "primary": True}]},
        "overrides": "overrides",
        "files": [],
    }
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        archive = root / "demo.zip"
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
            zipped.writestr("modlist.html", '<a href="https://example.test/mod">Example Mod</a>')
            zipped.writestr("overrides/mods/example.jar", b"jar")
            zipped.writestr("overrides/config/ftbquests/quests/chapters/intro.snbt", 'title: "Intro"\ndescription: ["Start"]')
            zipped.writestr("overrides/kubejs/server_scripts/recipes.js", 'event.shaped("minecraft:stick", ["A"], {A: "minecraft:apple"})')
        data = extract(archive, root / "exports", root / "manual")
        assert_equal("layout_kind", data["layout"]["kind"], "curseforge")
        assert_equal("quest_files", data["stats"]["quest_chapter_files"], 1)
        assert_equal("kubejs_scripts", data["stats"]["kubejs_scripts"], 1)
        inventory_record = next(record for record in data["records"] if str(record["path"]).endswith("Demo_CurseForge_Pack_pack_internal_inventory.md"))
        inventory_text = Path(str(inventory_record["path"])).read_text(encoding="utf-8")
        assert_true("overrides_root", "instance_root: overrides/" in inventory_text)
        assert_true("minecraft_version", "minecraft: 1.21.1" in inventory_text)


def test_modpack_internal_detects_modrinth_overrides_layout() -> None:
    import tempfile  # noqa: PLC0415
    import zipfile  # noqa: PLC0415

    from scripts.extract_modpack_internals import extract  # noqa: PLC0415

    index = {
        "formatVersion": 1,
        "game": "minecraft",
        "name": "Demo Modrinth Pack",
        "versionId": "1.2.3",
        "dependencies": {"minecraft": "1.20.1", "fabric-loader": "0.16.9"},
        "files": [
            {
                "path": "mods/example.jar",
                "downloads": ["https://cdn.modrinth.com/data/demo/example.jar"],
                "fileSize": 123,
            }
        ],
    }
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        archive = root / "demo.mrpack"
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("modrinth.index.json", json.dumps(index, ensure_ascii=False))
            zipped.writestr("overrides/mods/example.jar", b"jar")
            zipped.writestr("overrides/config/ftbquests/quests/chapters/intro.snbt", 'title: "Start Here"\ndescription: ["Begin"]\nitem: "minecraft:apple"')
            zipped.writestr("overrides/kubejs/server_scripts/recipes.js", 'event.shaped("minecraft:stick", ["A"], {A: "minecraft:apple"})')
        data = extract(archive, root / "exports", root / "manual")
        assert_equal("layout_kind", data["layout"]["kind"], "modrinth")
        assert_equal("layout_root", data["layout"]["root"], "overrides/")
        assert_equal("quest_files", data["stats"]["quest_chapter_files"], 1)
        assert_equal("kubejs_scripts", data["stats"]["kubejs_scripts"], 1)
        inventory_record = next(record for record in data["records"] if str(record["path"]).endswith("Demo_Modrinth_Pack_pack_internal_inventory.md"))
        inventory_text = Path(str(inventory_record["path"])).read_text(encoding="utf-8")
        assert_true("modrinth_version", "minecraft: 1.20.1" in inventory_text)
        assert_true("modrinth_loader", "fabric-loader=0.16.9" in inventory_text)
        assert_true("modrinth_files", "modrinth_files: 1" in inventory_text)


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


def test_jobs_payload_is_lightweight_and_does_not_refresh_manifest_files() -> None:
    original_jobs = dict(web_server.JOBS)
    original_order = list(web_server.JOBS_ORDER)
    original_manifest_stats = web_server._crawler_manifest_stats
    huge_output = "A" * 200_000

    def fail_manifest_refresh(_export_dir: str) -> dict:
        raise AssertionError("/api/jobs must not refresh manifest files while building the polling payload")

    job = web_server.Job(
        id="light-jobs-payload",
        kind="crawler",
        title="Crawler",
        status="running",
        created_at=1.0,
        started_at=1.0,
        summary="running",
        result={
            "plan": {"topic": "通用技术资料采集", "delivery_target": "human"},
            "planned_tasks": [{"source": "web_discovery", "query": "asyncio TaskGroup"}],
            "tasks": [
                {
                    "source": "web_discovery",
                    "query": "asyncio TaskGroup",
                    "output": huge_output,
                    "export_dir": "runtime/missing-manifest-dir",
                    "manifest_stats": {"records": 1},
                    "observation": {"status": "ok", "summary": "accepted"},
                    "topic_validation": {"reason": "CrawlerAgent accepted relevant technical content."},
                    "ingest_deferred": True,
                }
            ],
        },
    )
    try:
        web_server._crawler_manifest_stats = fail_manifest_refresh  # type: ignore[assignment]
        with web_server.JOBS_LOCK:
            web_server.JOBS.clear()
            web_server.JOBS_ORDER.clear()
            web_server.JOBS[job.id] = job
            web_server.JOBS_ORDER.append(job.id)
        payload = web_server._jobs_payload()
    finally:
        web_server._crawler_manifest_stats = original_manifest_stats  # type: ignore[assignment]
        with web_server.JOBS_LOCK:
            web_server.JOBS.clear()
            web_server.JOBS.update(original_jobs)
            web_server.JOBS_ORDER[:] = original_order

    jobs = payload.get("jobs") if isinstance(payload, dict) else []
    assert_equal("job_count", len(jobs), 1)
    task = jobs[0]["result"]["tasks"][0]
    assert_true("output_truncated", len(task.get("output") or "") < 2000)
    assert_true("readable_present", bool(jobs[0].get("readable", {}).get("self_audit")))


def test_modpack_download_evidence_is_manifest_fact_recall() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "mcagent.sqlite"
        conn = connect(db_path)
        init_db(conn)
        try:
            document = RawDocument(
                source_ref="evidence",
                source_path=Path(tmp) / "downloaded_archive_evidence_1.md",
                title="Downloaded modpack archive evidence",
                text=(
                    "# Downloaded modpack archive evidence\n\n"
                    "<!-- source: modpack_download_evidence -->\n"
                    "- query: 乌托邦探险之旅 / Utopian Journey 整合包 .mrpack .zip\n"
                    "- direct_archive_url: https://cnb.cool/minepixel.top/test/-/releases/download/test/MinePIxelWuTuoBang3.5.1Fix.zip\n"
                    "- bytes: 2173283032\n"
                    "- sha256: 5479e238489b9d2ec232de43d99797a76bedc277d6814a60f8bb2313f1fe9cce\n"
                ),
                metadata={},
            )
            replace_document(
                conn,
                document,
                [
                    TextChunk(
                        document_source_ref="evidence",
                        chunk_index=0,
                        text=document.text,
                        start_char=0,
                        end_char=len(document.text),
                        token_estimate=80,
                        metadata={},
                    )
                ],
            )
            chunk_ids = retriever._modpack_manifest_fact_chunk_ids(conn, "乌托邦探险之旅这个整合包的包体来源、大小和SHA256是什么？", limit=5)
            assert_equal("download_evidence_recalled", len(chunk_ids), 1)
            row = fetch_chunks_by_ids(conn, chunk_ids)[chunk_ids[0]]
            assert_true("download_evidence_boost", retriever._manifest_fact_boost(row, "乌托邦探险之旅 包体 来源 大小 SHA256") >= 6.0)
        finally:
            conn.close()


def test_local_modpack_archive_fact_answer_uses_download_evidence() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        evidence_path = Path(tmp) / "downloaded_archive_evidence_1.md"
        evidence_path.write_text(
            "\n".join(
                [
                    "# Downloaded modpack archive evidence",
                    "<!-- source: modpack_download_evidence -->",
                    "- query: 乌托邦探险之旅 / Utopian Journey 整合包 .mrpack .zip",
                    "- filename: MinePIxelWuTuoBang3.5.1Fix.zip",
                    "- source_page_or_metadata_endpoint: https://www.minepixel.top/dw/get.txt",
                    "- direct_archive_url: https://cnb.cool/minepixel.top/test/-/releases/download/test/MinePIxelWuTuoBang3.5.1Fix.zip",
                    "- probe_status: 206",
                    "- probe_content_type: application/zip",
                    "- probe_content_range: bytes 0-4095/2173283032",
                    "- probe_magic_hex: 504b030414000000",
                    "- archive_magic: zip",
                    "- bytes: 2173283032",
                    "- sha256: 5479e238489b9d2ec232de43d99797a76bedc277d6814a60f8bb2313f1fe9cce",
                ]
            ),
            encoding="utf-8",
        )
        result = SearchResult(
            rank=1,
            score=9.0,
            chunk_id=1,
            document_id=1,
            chunk_index=0,
            title="Downloaded modpack archive evidence",
            source_path=str(evidence_path),
            url=None,
            text=evidence_path.read_text(encoding="utf-8"),
        )
        answer = web_server._local_modpack_archive_fact_answer("乌托邦探险之旅这个整合包的包体来源、大小和SHA256是什么？", [result])
    assert_true("archive_url", "https://cnb.cool/minepixel.top/test/-/releases/download/test/MinePIxelWuTuoBang3.5.1Fix.zip" in answer)
    assert_true("archive_bytes", "2173283032" in answer)
    assert_true("archive_sha", "5479e238489b9d2ec232de43d99797a76bedc277d6814a60f8bb2313f1fe9cce" in answer)
    assert_true("archive_probe", "Content-Range" in answer and "504b030414000000" in answer)


def test_guide_question_prefers_mechanics_chunk_over_listing_chunk() -> None:
    farmers_delight = "\u519c\u592b\u4e50\u4e8b"
    question = farmers_delight + " Farmer's Delight \u65b0\u624b\u5e94\u8be5\u600e\u6837\u5f00\u59cb\uff1f\u8bf7\u7ed9\u51fa\u6765\u6e90\u7ebf\u7d22\u3002"
    listing_chunk = SearchResult(
        rank=1,
        score=0.95,
        chunk_id=1,
        document_id=1,
        chunk_index=0,
        title=f"[FD]{farmers_delight} (Farmer's Delight)",
        source_path=r"D:\case\crawler_exports\mcmod\accepted_by_crawler\mcmod_class_FD-Farmer-s-Delight---MC-Minecraft-MOD.md",
        url="https://www.mcmod.cn/class/2820.html",
        text=(
            "\u8fd0\u884c\u73af\u5883\uff1a\u5ba2\u6237\u7aef\u9700\u88c5\uff0c\u670d\u52a1\u7aef\u9700\u88c5\u3002"
            "\u4f9d\u8d56\u519c\u592b\u4e50\u4e8b\u7684 Mod \u5217\u8868\uff1aAppleSkin\u3001Corn Delight\u3001Nether's Delight\u3002"
            "\u5173\u7cfb\u7c7b\u578b\uff1a\u524d\u7f6e\u3001\u9644\u5c5e\u3001\u8054\u52a8\u3002\u8bc4\u5206\u3001\u4e0b\u8f7d\u6b21\u6570\u3001\u7f16\u8f91\u8d44\u6599\u3002"
        ),
    )
    mechanics_chunk = SearchResult(
        rank=2,
        score=0.72,
        chunk_id=2,
        document_id=1,
        chunk_index=3,
        title=f"[FD]{farmers_delight} (Farmer's Delight)",
        source_path=listing_chunk.source_path,
        url=listing_chunk.url,
        text=(
            "\u672c\u6a21\u7ec4\u6ca1\u6709\u6e38\u620f\u5185\u7684\u6307\u5bfc\u624b\u518c\uff0c\u53ef\u4ee5\u8ddf\u968f\u8fdb\u5ea6\u63d0\u793a\uff0c\u9ed8\u8ba4\u6309 L \u6253\u5f00\u8fdb\u5ea6\u754c\u9762\u3002"
            "\u63a2\u7d22\u4e16\u754c\u65f6\u4f1a\u627e\u5230\u91ce\u751f\u4f5c\u7269\uff0c\u5148\u91c7\u96c6\u6837\u672c\u6216\u79cd\u5b50\uff0c\u5efa\u9020\u4e00\u4e2a\u5c0f\u519c\u573a\u3002"
            "\u524d\u671f\u53ef\u4ee5\u5148\u505a\u71e7\u77f3\u5200\uff0c\u7528\u5200\u548c\u7827\u677f\u5904\u7406\u98df\u6750\u3002"
            "\u53a8\u9505\u548c\u714e\u9505\u9700\u8981\u70ed\u6e90\uff0c\u53a8\u9505 GUI \u548c\u5408\u6210\u4e66\u80fd\u5e2e\u4f60\u67e5\u70f9\u996a\u914d\u65b9\u3002"
        ),
    )
    unrelated = SearchResult(
        rank=3,
        score=0.65,
        chunk_id=3,
        document_id=2,
        chunk_index=0,
        title="AppleSkin",
        source_path=r"D:\case\crawler_exports\mcmod\mod_appleskin.md",
        url="https://www.mcmod.cn/class/744.html",
        text="AppleSkin \u4f1a\u5728 HUD \u663e\u793a\u9965\u997f\u503c\u548c\u98df\u7269\u6548\u679c\u3002",
    )
    selected = web_server._filter_answer_evidence_with_recovery(
        question,
        [listing_chunk, mechanics_chunk, unrelated],
        [listing_chunk, mechanics_chunk, unrelated],
        3,
    )
    assert_true("keeps_mechanics_chunk", selected and selected[0].chunk_id == mechanics_chunk.chunk_id, [item.chunk_id for item in selected])
    answer = web_server._local_extractive_answer(question, selected, fast=True)
    assert_true("answer_mentions_progress_or_tools", any(term in answer for term in ("\u8fdb\u5ea6", "\u7827\u677f", "\u5200", "\u53a8\u9505", "\u91ce\u751f\u4f5c\u7269")), answer)
    assert_true("answer_avoids_listing_only", "\u4f9d\u8d56\u519c\u592b\u4e50\u4e8b\u7684 Mod \u5217\u8868" not in answer, answer)


if __name__ == "__main__":
    test_direct_crawler_no_save_url_uses_temporary_extract_boundary()
    test_grounded_answer_does_not_fallback_to_ollama_after_profile_error()
    test_auto_max_tokens_uses_bounded_adaptive_limit()
    test_version_fact_answer_requires_subject_in_title_or_source()
    test_direct_user_handoff_brief_rejects_wrong_mcagent_identity()
    test_direct_crawler_delegate_choice_is_corrected_to_temporary_extract()
    test_direct_crawler_no_save_without_url_discovers_then_temporary_extracts()
    test_user_requested_output_dir_gets_final_delivery_files()
    test_mcagent_context_focus_expands_minecraft_utopia_aliases()
    test_mcagent_context_focus_keeps_gap_dimension_without_meta_instruction()
    test_mcagent_context_focus_compacts_inventory_noise_to_entity_and_dimensions()
    test_successful_mcagent_context_prunes_duplicate_pending_context_tasks()
    test_successful_mcagent_context_filters_new_duplicate_context_tasks()
    test_reflection_local_source_request_prevents_context_skip()
    test_reflection_local_evidence_wording_requests_materialization()
    test_materializes_local_source_path_tasks_after_mcagent_context_reflection()
    test_mcagent_gap_delegation_overrides_human_delivery_to_rag()
    test_delegate_confirmation_can_cancel_background_job()
    test_explicit_mcagent_to_crawler_handoff_starts_job_after_agent_selects_delegate()
    test_recent_crawler_audit_question_answers_history_without_new_collection()
    test_recent_crawler_audit_question_matches_create_instead_of_higher_activity_job()
    test_crawler_collection_request_message_does_not_force_tool_choice()
    test_crawler_collection_request_preserves_mcagent_context_choice_without_forcing_delegate()
    test_crawler_collection_request_message_starts_job_after_crawler_selects_delegate()
    test_direct_crawler_planned_workflow_preserves_selected_action_plan_for_job()
    test_direct_crawler_mcagent_context_step_with_delegate_starts_background_job()
    test_explicit_mcagent_to_crawler_handoff_relays_before_heavy_router()
    test_explicit_handoff_with_source_audit_requirements_still_relays_fast()
    test_modrinth_plain_mod_task_does_not_parse_modpack_contents()
    test_known_modrinth_project_skips_are_reusable_existing_evidence()
    test_modrinth_slug_query_is_direct_project_candidate()
    test_modrinth_explicit_modpack_manifest_task_can_parse_contents()
    test_crawler_job_can_execute_mcagent_context_tool()
    test_mcagent_context_request_does_not_recursively_delegate_crawler()
    test_mcagent_context_tool_timeout_returns_objective_blocker()
    test_mcagent_context_filters_off_topic_local_evidence()
    test_specific_utopian_journey_filter_rejects_generic_utopian_sources()
    test_specific_utopian_journey_filter_rejects_other_pack_mentions()
    test_utopia_journey_filter_keeps_real_chinese_pack_and_rejects_aoa_armor()
    test_version_install_extraction_ignores_mcmod_navigation_loaders()
    test_local_version_install_answer_ignores_wrong_modpack_sources()
    test_no_llm_mcagent_path_still_runs_evidence_selection()
    test_version_install_note_extracts_modpack_requirements()
    test_modpack_overview_surfaces_version_install_evidence()
    test_agent_selected_local_corpus_inventory_route_executes_inventory_tool()
    test_inventory_route_review_cannot_add_unselected_delegate_side_effect()
    test_local_corpus_inventory_is_not_keyword_forced_before_agent_choice()
    test_status_runs_only_after_agent_selects_status_tool()
    test_agent_selected_crawler_audit_route_reads_audit_tool()
    test_chat_router_does_not_force_agent_tool_choice_with_keyword_fast_paths()
    test_general_answer_path_skips_local_fact_answer_for_modpack_overview()
    test_pseudo_delegate_call_in_final_answer_is_removed_without_late_side_effect()
    test_unselected_pseudo_tool_text_is_removed_without_side_effect()
    test_local_rag_route_review_cannot_add_unselected_delegate_side_effect()
    test_conditional_delegate_suggestion_is_allowed_without_side_effect()
    test_mcagent_context_tool_uses_message_bus_instead_of_internal_mcagent_shortcut()
    test_mcagent_to_crawler_delegation_uses_message_bus_not_job_starter()
    test_mcagent_context_request_message_gets_light_reply_without_recursive_job()
    test_chat_runtime_timeout_returns_objective_blocker()
    test_delegate_handoff_brief_uses_bounded_llm_timeout()
    test_crawler_topic_match_decision_comes_from_crawler_llm()
    test_crawler_summary_uses_only_llm_matched_record_indexes()
    test_zero_byte_artifact_is_visible_but_not_accepted_for_ingest()
    test_structured_manifest_records_count_as_usable_objective_content()
    test_manifest_preview_filters_encoding_damaged_fields()
    test_job_readable_refreshes_legacy_manifest_stats()
    test_light_job_plan_preserves_model_prior_boundary()
    test_duplicate_reuse_requires_crawler_llm_acceptance()
    test_modpack_internal_missing_archive_reports_objective_blocker()
    test_modpack_download_accepts_direct_archive_url_as_candidate()
    test_archive_url_helper_and_fetch_url_boundary()
    test_modpack_download_direct_archive_url_is_range_probed()
    test_modpack_download_modrinth_search_uses_clean_alias_variant()
    test_modpack_download_skips_download_when_candidate_name_mismatches_target()
    test_modpack_download_discovers_curseforge_mediafilez_candidate()
    test_modpack_download_search_queries_use_readable_chinese_terms()
    test_encoding_damage_guard_catches_mojibake_without_blocking_valid_chinese()
    test_modpack_download_reports_bbsmc_cloud_drive_blocker()
    test_modpack_download_has_bounded_probe_timeout()
    test_mcmod_search_has_bounded_task_budget()
    test_public_discovery_tools_have_bounded_task_budget()
    test_crawler_reflection_timeout_continues_with_pending_task()
    test_crawler_reflection_timeout_continues_low_yield_when_pending_exists()
    test_modpack_download_defaults_to_probe_only_command()
    test_modpack_download_allows_explicit_full_download_command()
    test_modpack_archive_lookup_skips_corrupt_zip()
    test_record_validation_skips_binary_archive_body()
    test_modpack_download_follows_public_release_seed()
    test_modpack_download_prioritizes_verified_public_release_candidate()
    test_modpack_download_filters_generic_google_archives()
    test_modpack_download_writes_download_evidence_markdown()
    test_modpack_download_decodes_mcmod_external_redirect_links()
    test_download_archive_reuses_valid_existing_zip()
    test_ranged_download_resumes_existing_part_file()
    test_modpack_internal_detects_minecraft_instance_layout()
    test_modpack_internal_uses_curseforge_overrides_root()
    test_modpack_internal_detects_modrinth_overrides_layout()
    test_job_to_dict_hides_unqualified_modpack_internal_from_history_view()
    test_jobs_payload_is_lightweight_and_does_not_refresh_manifest_files()
    test_modpack_download_evidence_is_manifest_fact_recall()
    test_local_modpack_archive_fact_answer_uses_download_evidence()
    test_guide_question_prefers_mechanics_chunk_over_listing_chunk()
    print("web_server_side_effect_guard_scenarios passed")
