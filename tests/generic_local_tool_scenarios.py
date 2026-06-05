from __future__ import annotations

from pathlib import Path
import shutil
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.read_local_file import read_file  # noqa: E402
from scripts.search_local_files import search_files  # noqa: E402
from scripts.fetch_url_seed import extract_url, html_to_markdown, save_url  # noqa: E402
import scripts.fetch_web_discovery_seed as web_discovery_seed  # noqa: E402


TMP = ROOT / "runtime" / "test_generic_local_tools"


def assert_equal(name: str, actual, expected) -> None:
    if actual != expected:
        raise AssertionError(f"{name}: expected {expected!r}, got {actual!r}")


def assert_true(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name}: {detail}")


def reset_tmp() -> None:
    if TMP.exists():
        shutil.rmtree(TMP)
    TMP.mkdir(parents=True, exist_ok=True)


def test_read_local_file_exports_manifest_and_markdown() -> None:
    reset_tmp()
    source = TMP / "source.txt"
    source.write_text("alpha beta gamma\n" * 20, encoding="utf-8")
    manifest = read_file(source, TMP / "exports", max_chars=120)
    assert_equal("records", len(manifest["records"]), 1)
    record = manifest["records"][0]
    assert_true("markdown_exists", Path(str(record["path"])).exists())
    assert_true("manifest_exists", (Path(str(manifest["export_dir"])) / "manifest.json").exists())


def test_search_local_files_exports_matches() -> None:
    reset_tmp()
    docs = TMP / "docs"
    docs.mkdir()
    (docs / "one.md").write_text("CrawlerAgent generic tools should find this target phrase.", encoding="utf-8")
    (docs / "two.md").write_text("Nothing related here.", encoding="utf-8")
    manifest = search_files(docs, "target phrase", TMP / "exports", max_files=5, max_chars=200)
    assert_equal("records", len(manifest["records"]), 1)
    assert_true("report_exists", Path(str(manifest["records"][0]["path"])).exists())
    assert_true("source_path", str(manifest["records"][0]["source_path"]).endswith("one.md"))


def test_fetch_url_helpers_are_local_and_generic() -> None:
    assert_equal("url_extract", extract_url("read https://example.com/a?x=1 please"), "https://example.com/a?x=1")
    title, text = html_to_markdown("<html><head><title>Example</title></head><body><h1>Hello</h1><p>Readable text.</p></body></html>", "text/html", "https://example.com")
    assert_equal("title", title, "Example")
    assert_true("text", "Readable text" in text)


def test_fetch_url_refuses_binary_modpack_archive_urls() -> None:
    reset_tmp()
    manifest = save_url("https://cdn.example.test/packs/demo.mrpack", TMP / "exports", timeout=1, user_agent="unit-test")
    assert_equal("records", len(manifest["records"]), 0)
    assert_equal("status", manifest["status"], "blocked")
    assert_equal("archive_url_detected", manifest["archive_url_detected"], True)
    assert_true("recommended_source", "modpack_download" in manifest["next_action"])


def test_web_discovery_manifest_exposes_objective_candidate_records() -> None:
    reset_tmp()
    originals = {
        "query_variants": web_discovery_seed.query_variants,
        "bing_rss_search": web_discovery_seed.bing_rss_search,
        "bing_html_search": web_discovery_seed.bing_html_search,
        "so_html_search": web_discovery_seed.so_html_search,
        "github_repo_search": web_discovery_seed.github_repo_search,
        "request_text": web_discovery_seed.request_text,
        "load_ledger": web_discovery_seed.load_ledger,
        "build_global_indexes": web_discovery_seed.build_global_indexes,
        "append_ledger": web_discovery_seed.append_ledger,
    }
    try:
        web_discovery_seed.query_variants = lambda query: [query]  # type: ignore[assignment]
        web_discovery_seed.bing_rss_search = lambda *args, **kwargs: [  # type: ignore[assignment]
            {
                "title": "Farmer's Delight - Minecraft Mod",
                "url": "https://modrinth.com/mod/farmers-delight",
                "snippet": "Farmer's Delight is a Minecraft farming and cooking mod.",
                "engine": "unit",
            }
        ]
        web_discovery_seed.bing_html_search = lambda *args, **kwargs: []  # type: ignore[assignment]
        web_discovery_seed.so_html_search = lambda *args, **kwargs: []  # type: ignore[assignment]
        web_discovery_seed.github_repo_search = lambda *args, **kwargs: []  # type: ignore[assignment]
        web_discovery_seed.request_text = lambda *args, **kwargs: (  # type: ignore[assignment]
            "<html><head><title>Farmer's Delight</title></head><body>"
            + "Farmer's Delight Minecraft mod cooking farming download versions " * 20
            + "</body></html>",
            "text/html",
            200,
        )
        web_discovery_seed.load_ledger = lambda: {}  # type: ignore[assignment]
        web_discovery_seed.build_global_indexes = lambda ledger: ({}, {})  # type: ignore[assignment]
        web_discovery_seed.append_ledger = lambda record: None  # type: ignore[assignment]
        manifest = web_discovery_seed.fetch_web_discovery(
            TMP / "exports",
            "Farmer's Delight Minecraft mod",
            "unit-test",
            max_results=5,
            max_pages=1,
            delay=0,
            force=True,
        )
    finally:
        for name, value in originals.items():
            setattr(web_discovery_seed, name, value)
    assert_equal("candidates_count", manifest["candidates"], 1)
    assert_equal("candidate_records", len(manifest["candidate_records"]), 1)
    assert_equal("expanded_candidate_records", len(manifest["expanded_candidate_records"]), 1)
    assert_true("candidate_url", manifest["candidate_records"][0]["url"].startswith("https://modrinth.com/"))


if __name__ == "__main__":
    test_read_local_file_exports_manifest_and_markdown()
    test_search_local_files_exports_matches()
    test_fetch_url_helpers_are_local_and_generic()
    test_fetch_url_refuses_binary_modpack_archive_urls()
    test_web_discovery_manifest_exposes_objective_candidate_records()
    print("generic_local_tool_scenarios passed")
