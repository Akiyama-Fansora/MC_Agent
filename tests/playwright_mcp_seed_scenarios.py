from __future__ import annotations

from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import playwright_mcp_seed  # noqa: E402
from scripts.playwright_mcp_seed import _relocate_browser_evidence, compact_text, fetch_playwright_mcp, neutral_query_variants, parse_actions, safe_slug  # noqa: E402


def assert_equal(name: str, actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"{name}: expected {expected!r}, got {actual!r}")


def assert_true(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name}: {detail}")


def test_parse_actions_accepts_inline_json_and_file() -> None:
    inline = parse_actions('[{"action":"click","selector":"button"}]')
    assert_equal("inline_len", len(inline), 1)
    assert_equal("inline_action", inline[0]["action"], "click")
    path = Path(tempfile.mkdtemp(prefix="playwright-mcp-actions-")) / "actions.json"
    path.write_text('{"actions":[{"action":"type","selector":"input","text":"hello"}]}', encoding="utf-8")
    from_file = parse_actions(str(path))
    assert_equal("file_len", len(from_file), 1)
    assert_equal("file_text", from_file[0]["text"], "hello")


def test_text_helpers_are_stable_for_artifact_names() -> None:
    assert_equal("slug", safe_slug("FastAPI BackgroundTasks / docs?"), "FastAPI-BackgroundTasks-docs")
    assert_equal("compact", compact_text("a\n  b\tc", 10), "a b c")


def test_neutral_query_variants_do_not_inject_domain_terms() -> None:
    variants = neutral_query_variants("FastAPI background tasks")
    joined = " ".join(variants).lower()
    assert_true("no_minecraft_injection", "minecraft" not in joined, detail=str(variants))
    assert_true("no_mcmod_injection", "mcmod" not in joined, detail=str(variants))


def test_relocate_browser_evidence_keeps_manifest_paths_inside_export_dir() -> None:
    temp = Path(tempfile.mkdtemp(prefix="playwright-mcp-relocate-"))
    export_dir = temp / "export"
    external = temp / "external"
    export_dir.mkdir()
    external.mkdir()
    snapshot = external / "snapshot.md"
    raw_page = external / "raw_page.html"
    screenshot = external / "page.png"
    actions = external / "actions.json"
    snapshot.write_text("snapshot", encoding="utf-8")
    raw_page.write_text("<html></html>", encoding="utf-8")
    screenshot.write_bytes(b"png")
    actions.write_text("{}", encoding="utf-8")
    md = export_dir / "record.md"
    md.write_text(f"paths\n{snapshot}\n{raw_page}\n{screenshot}\n{actions}\n", encoding="utf-8")
    manifest = {
        "export_dir": str(export_dir),
        "records": [
            {
                "title": "Demo Page",
                "path": str(md),
                "metadata": {
                    "raw_page_path": str(raw_page),
                    "snapshot_path": str(snapshot),
                    "screenshot_path": str(screenshot),
                    "actions_path": str(actions),
                },
            }
        ],
    }
    _relocate_browser_evidence(manifest)
    metadata = manifest["records"][0]["metadata"]
    assert_true("raw_page_inside", str(export_dir) in metadata["raw_page_path"], metadata["raw_page_path"])
    assert_true("snapshot_inside", str(export_dir) in metadata["snapshot_path"], metadata["snapshot_path"])
    assert_true("screenshot_inside", str(export_dir) in metadata["screenshot_path"], metadata["screenshot_path"])
    assert_true("actions_inside", str(export_dir) in metadata["actions_path"], metadata["actions_path"])
    assert_true("markdown_rewritten", str(export_dir) in md.read_text(encoding="utf-8"))


def test_low_relevance_pages_are_exported_for_agent_judgment() -> None:
    temp = Path(tempfile.mkdtemp(prefix="playwright-mcp-low-score-"))

    def fake_search_candidates(query: str, user_agent: str, max_results: int):  # noqa: ANN001
        return ([{"title": "Unrelated", "url": "https://example.test/low", "snippet": "", "query": query}], [])

    def fake_render_page(*args: object, **kwargs: object) -> dict[str, object]:
        page_dir = Path(kwargs["output_dir"])
        page_dir.mkdir(parents=True, exist_ok=True)
        paths = {
            "raw_html": str(page_dir / "raw_page.html"),
            "snapshot": str(page_dir / "snapshot.md"),
            "screenshot": str(page_dir / "page.png"),
            "console": str(page_dir / "console.json"),
            "network": str(page_dir / "network.json"),
            "actions": str(page_dir / "actions.json"),
        }
        for key, path in paths.items():
            target = Path(path)
            if key == "screenshot":
                target.write_bytes(b"png")
            else:
                target.write_text("{}" if key in {"console", "network", "actions"} else key, encoding="utf-8")
        return {"title": "Unrelated", "final_url": "https://example.test/low", "text": "x", "html": "<html>x</html>", "snapshot": "snapshot", "paths": paths}

    original_search = playwright_mcp_seed.search_candidates
    original_render = playwright_mcp_seed.render_page
    original_score = playwright_mcp_seed.relevance_score
    try:
        playwright_mcp_seed.search_candidates = fake_search_candidates
        playwright_mcp_seed.render_page = fake_render_page
        playwright_mcp_seed.relevance_score = lambda *args, **kwargs: 0.0
        manifest = fetch_playwright_mcp(temp, "agent decides", 1, 1, True, "ua", 5000, False, [], 3)
    finally:
        playwright_mcp_seed.search_candidates = original_search
        playwright_mcp_seed.render_page = original_render
        playwright_mcp_seed.relevance_score = original_score

    assert_equal("low_score_records", len(manifest["records"]), 1)
    assert_equal("low_score_skipped", len(manifest["skipped"]), 0)
    assert_equal("low_score_value", manifest["records"][0]["score"], 0.0)


if __name__ == "__main__":
    test_parse_actions_accepts_inline_json_and_file()
    test_text_helpers_are_stable_for_artifact_names()
    test_neutral_query_variants_do_not_inject_domain_terms()
    test_relocate_browser_evidence_keeps_manifest_paths_inside_export_dir()
    test_low_relevance_pages_are_exported_for_agent_judgment()
    print("playwright_mcp_seed_scenarios passed")
