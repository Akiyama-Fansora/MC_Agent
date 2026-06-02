from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.config import AppConfig, ChunkingConfig, EmbeddingConfig, OllamaConfig, PathsConfig, RetrievalConfig  # noqa: E402
from mcagent.ingest import ingest_exports  # noqa: E402
from mcagent.retriever import Retriever  # noqa: E402
from mcagent.session_state import InMemorySessionStore  # noqa: E402
import mcagent.web_server as web_server  # noqa: E402


def build_config(root: Path, source: Path) -> AppConfig:
    return AppConfig(
        paths=PathsConfig(
            project_root=root,
            source_dir=source,
            db_path=root / "mcagent.sqlite",
            index_path=root / "vector_index.npz",
        ),
        embedding=EmbeddingConfig(dimension=512, ngram_min=1, ngram_max=4),
        chunking=ChunkingConfig(max_chars=700, overlap_chars=120),
        retrieval=RetrievalConfig(top_k=4, min_score=0.0),
        ollama=OllamaConfig(),
    )


def assert_true(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name}: {detail}")


def test_modpack_manifest_facts_are_preferred_over_release_filename() -> None:
    with tempfile.TemporaryDirectory(prefix="mcagent-manifest-") as tmp:
        root = Path(tmp)
        source = root / "crawler_exports" / "vefc_case"
        source.mkdir(parents=True)
        (source / "route_draft_test.md").write_text(
            "# 香草纪元:食旅纪行 route evidence draft\n\n这是整合包路线证据。",
            encoding="utf-8",
        )
        (source / "modpack_archive_summary_test.json").write_text(
            json.dumps(
                {
                    "archive_path": r"D:\packs\香草纪元食旅纪行-Release1.9.0FIX.zip",
                    "manifest_summaries": [
                        {
                            "path": "manifest.json",
                            "kind": "curseforge_manifest",
                            "name": "VanillaEra:FaresChron",
                            "minecraft_version": "1.20.1",
                            "modloaders": [{"id": "forge-47.3.22", "primary": True}],
                        }
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        (source / "modpack_manifests_test.json").write_text(
            json.dumps(
                [
                    {
                        "path": "manifest.json",
                        "kind": "curseforge_manifest",
                        "parsed": {
                            "minecraft": {
                                "version": "1.20.1",
                                "modLoaders": [{"id": "forge-47.3.22", "primary": True}],
                            },
                            "manifestType": "minecraftModpack",
                            "manifestVersion": 1,
                            "name": "VanillaEra:FaresChron",
                            "files": [{"projectID": 1, "fileID": 2, "required": True}],
                        },
                    }
                ],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        config = build_config(root, root / "crawler_exports")
        stats = ingest_exports(config)
        assert_true("docs_loaded", stats.documents_loaded >= 4, str(stats))

        results = Retriever(config).search("香草纪元:食旅纪行 使用什么 Minecraft 版本和加载器？", top_k=3)
        assert_true("has_results", bool(results))
        combined = "\n".join(item.title + "\n" + item.text for item in results)
        assert_true("manifest_fact_present", "Minecraft 版本: 1.20.1" in combined, combined)
        assert_true("loader_fact_present", "forge-47.3.22" in combined, combined)
        assert_true("release_warning_present", "不要从压缩包文件名" in combined, combined)


def test_crawler_accepted_sources_are_preferred_for_project_questions() -> None:
    with tempfile.TemporaryDirectory(prefix="mcagent-crawler-accepted-") as tmp:
        root = Path(tmp)
        source = root / "crawler_exports"
        accepted = source / "fetch_url" / "case" / "accepted_by_crawler"
        noise = source / "modrinth_agent" / "old"
        accepted.mkdir(parents=True)
        noise.mkdir(parents=True)
        (accepted / "Farmer-s-Delight.md").write_text(
            "# Farmer's Delight\n\n"
            "农夫乐事 Farmer's Delight 是一个围绕烹饪锅、煎锅、刀、砧板、食物和农作物展开的 Minecraft 模组。\n"
            "项目页: https://modrinth.com/mod/farmers-delight\n"
            "MC百科: https://www.mcmod.cn/class/2820.html\n",
            encoding="utf-8",
        )
        (noise / "Shady-GUI-Farmers-Delight.md").write_text(
            "# Shady's Dark Okami GUI - Farmer's Delight\n\n"
            "This is a GUI resource pack extension for Farmer's Delight, not the main mod page.\n",
            encoding="utf-8",
        )
        manifest_noise = source / "vefc" / "modpack_manifests_noise.json"
        manifest_noise.parent.mkdir(parents=True)
        manifest_noise.write_text(
            json.dumps(
                {
                    "source_kind": "modpack_manifest_facts",
                    "name": "VanillaEra:FaresChron",
                    "minecraft_version": "1.20.1",
                    "modloader": "forge-47.3.22",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        config = build_config(root, source)
        stats = ingest_exports(config)
        assert_true("docs_loaded", stats.documents_loaded >= 2, str(stats))

        extra = source / "extra" / "accepted_by_crawler"
        extra.mkdir(parents=True)
        (extra / "one.md").write_text("# One\n\nCrawler accepted delta one.", encoding="utf-8")
        (extra / "two.md").write_text("# Two\n\nCrawler accepted delta two.", encoding="utf-8")
        limited_stats = ingest_exports(config, allowed_roots=[extra], incremental_index_chunk_limit=1)
        assert_true("limited_target_chunks", limited_stats.index_target_chunks >= 2, str(limited_stats))
        assert_true("limited_pending_chunks", limited_stats.index_pending_chunks >= 1, str(limited_stats))
        assert_true("delta_index_exists", config.paths.index_path.with_name("vector_index.delta.npz").exists())

        results = Retriever(config).search("Farmer's Delight 农夫乐事 玩法 烹饪 项目页", top_k=2)
        assert_true("has_results", bool(results))
        top_path = results[0].source_path.replace("\\", "/")
        assert_true("accepted_source_first", "/accepted_by_crawler/" in top_path, "\n".join(item.source_path for item in results))
        assert_true("main_mod_content", "烹饪锅" in results[0].text and "项目页" in results[0].text, results[0].text)
        combined_paths = "\n".join(item.source_path for item in results)
        assert_true("other_modpack_manifest_not_selected", "modpack_manifests_noise" not in combined_paths, combined_paths)


def test_session_memory_is_scoped_by_session_id_and_supports_followups() -> None:
    original_store = web_server.SESSION_STORE
    store = InMemorySessionStore()
    web_server.SESSION_STORE = store
    try:
        web_server._append_session(
            {"session_id": "session-a"},
            "香草纪元:食旅纪行新手怎么玩？",
            "香草纪元:食旅纪行先做新手礼包和指引任务。",
            [],
        )
        web_server._append_session(
            {"session_id": "session-b"},
            "乌托邦整合包新手怎么玩？",
            "乌托邦整合包先确认版本和任务线。",
            [],
        )

        summary_a = web_server._session_summary({"session_id": "session-a"})
        summary_b = web_server._session_summary({"session_id": "session-b"})
        assert_true("session_a_mentions_vefc", "香草纪元" in json.dumps(summary_a, ensure_ascii=False), str(summary_a))
        assert_true("session_b_is_isolated", "香草纪元" not in json.dumps(summary_b, ensure_ascii=False), str(summary_b))

        rewritten, note, changed = web_server._contextualize_question({"session_id": "session-a"}, "这些后期目标有哪些？")
        assert_true("followup_rewritten", changed, rewritten)
        assert_true("followup_keeps_topic", "香草纪元" in rewritten or "香草纪元" in note, rewritten + "\n" + note)
    finally:
        web_server.SESSION_STORE = original_store


def main() -> int:
    test_modpack_manifest_facts_are_preferred_over_release_filename()
    test_crawler_accepted_sources_are_preferred_for_project_questions()
    test_session_memory_is_scoped_by_session_id_and_supports_followups()
    print("MODPACK MANIFEST AND SESSION TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
