from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.artifact_reference_service import ArtifactReferenceService  # noqa: E402


TMP = ROOT / "runtime" / "test_artifact_refs"


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


def write_manifest() -> tuple[Path, Path]:
    reset_tmp()
    doc = TMP / "doc.md"
    raw = TMP / "raw.html"
    doc.write_text("# Page\n\nUseful text.", encoding="utf-8")
    raw.write_text("<html><body>Useful text.</body></html>", encoding="utf-8")
    manifest = {
        "records": [
            {
                "title": "Example Page",
                "url": "https://example.test/page",
                "path": str(doc),
                "raw_html_path": str(raw),
            }
        ],
        "errors": [],
        "skipped": [],
    }
    (TMP / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return doc, raw


def test_collect_refs_from_manifest() -> None:
    doc, raw = write_manifest()
    refs = ArtifactReferenceService().collect_from_result(
        result={"source": "fetch_url", "query": "example", "export_dir": str(TMP), "manifest_stats": {"manifest_path": str(TMP / "manifest.json")}},
        result_index=1,
    )
    assert_equal("refs_count", len(refs), 2)
    assert_equal("first_id", refs[0]["id"], "r1.1")
    assert_equal("first_path", refs[0]["path"], str(doc))
    assert_equal("second_path", refs[1]["path"], str(raw))
    assert_equal("text_like", refs[0]["text_like"], True)


def test_resolve_latest_ref_into_payload_content() -> None:
    write_manifest()
    service = ArtifactReferenceService()
    refs = service.collect_from_result(result={"source": "fetch_url", "query": "example", "export_dir": str(TMP)}, result_index=2)
    payload = service.resolve_payload_refs({"content_ref": "latest:md", "metadata": {"purpose": "copy"}}, refs)
    assert_true("content_loaded", "Useful text" in payload["content"])
    assert_equal("metadata_ref", payload["metadata"]["resolved_artifact_ref"]["id"], "r2.1")


def test_relative_manifest_paths_resolve_from_manifest_directory() -> None:
    reset_tmp()
    raw_dir = TMP / "raw_html"
    raw_dir.mkdir(parents=True, exist_ok=True)
    doc = TMP / "relative.md"
    raw = raw_dir / "relative.html"
    doc.write_text("# Relative\n\nContent loaded from a relative manifest path.", encoding="utf-8")
    raw.write_text("<html><body>Relative raw HTML.</body></html>", encoding="utf-8")
    manifest = {
        "records": [
            {
                "title": "Relative Page",
                "url": "https://example.test/relative",
                "path": "relative.md",
                "raw_html_path": "raw_html/relative.html",
            }
        ]
    }
    (TMP / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    service = ArtifactReferenceService()
    refs = service.collect_from_result(
        result={"source": "fetch_url", "query": "relative", "manifest_stats": {"manifest_path": str(TMP / "manifest.json")}},
        result_index=3,
    )
    payload = service.resolve_payload_refs({"content_ref": "latest:md"}, refs)

    assert_equal("relative_refs_count", len(refs), 2)
    assert_equal("relative_doc_path", refs[0]["path"], str(doc.resolve()))
    assert_equal("relative_raw_path", refs[1]["path"], str(raw.resolve()))
    assert_true("relative_content_loaded", "relative manifest path" in payload["content"])


def test_missing_ref_sets_objective_error() -> None:
    write_manifest()
    payload = ArtifactReferenceService().resolve_payload_refs({"artifact_ref": "r9.9"}, [])
    assert_true("error_set", "Artifact reference not found" in payload["content_ref_error"])


def main() -> int:
    test_collect_refs_from_manifest()
    test_resolve_latest_ref_into_payload_content()
    test_relative_manifest_paths_resolve_from_manifest_directory()
    test_missing_ref_sets_objective_error()
    print("artifact_reference_service_scenarios passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
