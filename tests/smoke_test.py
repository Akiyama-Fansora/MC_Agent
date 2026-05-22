from __future__ import annotations

from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.chat import answer_question
from mcagent.config import (  # noqa: E402
    AppConfig,
    ChunkingConfig,
    EmbeddingConfig,
    OllamaConfig,
    PathsConfig,
    RetrievalConfig,
)
from mcagent.agent_runtime import (  # noqa: E402
    build_handoff_contract,
    classify_crawler_tool_result,
    crawler_collection_catalog_prompt,
    tool_catalog_prompt,
    tool_names_for_agent,
    validate_tool_name,
)
from mcagent.ingest import ingest_exports  # noqa: E402
from mcagent.retriever import Retriever  # noqa: E402


def build_test_config(root: Path, source_dir: Path) -> AppConfig:
    return AppConfig(
        paths=PathsConfig(
            project_root=root,
            source_dir=source_dir,
            db_path=root / "mcagent.sqlite",
            index_path=root / "vector_index.npz",
        ),
        embedding=EmbeddingConfig(dimension=512, ngram_min=1, ngram_max=4),
        chunking=ChunkingConfig(max_chars=500, overlap_chars=80),
        retrieval=RetrievalConfig(top_k=3, min_score=0.0),
        ollama=OllamaConfig(),
    )


def main() -> int:
    mc_tools = set(tool_names_for_agent("mcagent_rag"))
    assert {"direct_answer", "local_rag_search", "delegate_crawler", "status"} <= mc_tools, mc_tools
    crawler_route_tools = set(tool_names_for_agent("crawler_agent"))
    assert {"direct_answer", "temporary_extract", "delegate_crawler", "status"} <= crawler_route_tools, crawler_route_tools
    collection_catalog = crawler_collection_catalog_prompt()
    assert "browser_collect" in collection_catalog, "Crawler collection catalog missing browser tool"
    assert "save_artifact" in collection_catalog, "Crawler collection catalog missing save artifact tool"
    assert validate_tool_name("crawler_agent", "answer", fallback="delegate_crawler") == "delegate_crawler"
    assert "LLM owns interpretation" in tool_catalog_prompt("mcagent_rag")
    assert classify_crawler_tool_result({"source": "tavily", "returncode": 124, "timed_out": True}).status == "timeout"
    assert classify_crawler_tool_result({"source": "firecrawl", "returncode": 1, "output": "HTTP 429 quota exceeded"}).status == "quota_limited"
    assert classify_crawler_tool_result({"source": "mcmod", "returncode": 0, "empty_result": True, "manifest_stats": {"records": 0}}).status == "empty"
    assert classify_crawler_tool_result({"source": "mcmod", "returncode": 0, "topic_validation": {"matched": True}, "manifest_stats": {"records": 2}}).status == "ok"
    contract = build_handoff_contract(
        requested_by="user_via_mcagent",
        from_agent="MCagent",
        to_agent="CrawlerAgent",
        user_request="让 Crawler 补资料",
        task_goal="补齐整合包 Boss 清单",
        delivery_target="MCagent/RAG",
        acceptance_criteria=["保存可引用资料", "说明失败原因"],
    )
    assert "Original user request" in contract.to_prompt_text()
    assert "补齐整合包 Boss 清单" in contract.to_prompt_text()

    with tempfile.TemporaryDirectory(prefix="mcagent-smoke-") as tmp:
        root = Path(tmp)
        empty_root = root / "empty_case"
        empty_source = empty_root / "crawler_exports"
        empty_source.mkdir(parents=True)
        empty_config = build_test_config(empty_root, empty_source)
        empty_stats = ingest_exports(empty_config)
        assert empty_stats.files_seen == 0, empty_stats
        assert empty_stats.documents_loaded == 0, empty_stats
        assert empty_stats.index_vectors == 0, empty_stats
        assert (empty_root / "mcagent.sqlite").exists()
        assert (empty_root / "vector_index.npz").exists()

        sample_root = root / "sample_case"
        source = sample_root / "crawler_exports"
        source.mkdir(parents=True)
        sample = source / "redstone.md"
        sample.write_text(
            "# 红石粉\n\n红石粉可以通过挖掘红石矿石获得，也可以用于铺设红石线路。\n\n红石信号最远可以传递 15 格。",
            encoding="utf-8",
        )

        config = build_test_config(sample_root, source)
        stats = ingest_exports(config)
        assert stats.files_seen == 1, stats
        assert stats.documents_loaded == 1, stats
        assert stats.index_vectors >= 1, stats

        results = Retriever(config).search("红石粉怎么获得？", top_k=1)
        assert results, "no search results"
        assert "红石矿石" in results[0].text, results[0].text

        # Exercise the no-LLM answer path without touching the real project config.
        config_path = sample_root / "config.json"
        config_path.write_text(
            "{"
            f"\"paths\":{{\"source_dir\":\"{source.as_posix()}\","
            f"\"db_path\":\"{(sample_root / 'mcagent.sqlite').as_posix()}\","
            f"\"index_path\":\"{(sample_root / 'vector_index.npz').as_posix()}\"}},"
            "\"embedding\":{\"provider\":\"hashing_char_ngram\",\"dimension\":512,\"ngram_min\":1,\"ngram_max\":4},"
            "\"chunking\":{\"max_chars\":500,\"overlap_chars\":80},"
            "\"retrieval\":{\"top_k\":3,\"min_score\":0.0}"
            "}",
            encoding="utf-8",
        )
        answer = answer_question("红石粉怎么获得？", config_path=str(config_path), no_llm=True)
        assert "来源：" in answer, answer
        assert "redstone.md" in answer, answer

    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
