from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.artifact_save_service import ArtifactSaveError, ArtifactSaveService  # noqa: E402


TMP = ROOT / "runtime" / "test_artifacts"


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


def test_save_markdown_to_directory_writes_manifest() -> None:
    reset_tmp()
    result = ArtifactSaveService().save(
        content="# Title\n\nSaved by CrawlerAgent.",
        artifact_format="md",
        path=TMP,
        filename="note",
        metadata={"source": "unit-test"},
    )
    saved = Path(result.path)
    manifest = Path(result.manifest_path)
    assert_true("saved_exists", saved.exists())
    assert_equal("saved_suffix", saved.suffix, ".md")
    assert_true("manifest_exists", manifest.exists())
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert_equal("provider", data["provider"], "save_artifact")
    assert_equal("records_count", len(data["records"]), 1)
    assert_equal("saved_to_local", result.saved_to_local, True)


def test_save_json_and_avoid_overwrite_by_default() -> None:
    reset_tmp()
    service = ArtifactSaveService()
    first = service.save(content={"a": 1}, artifact_format="json", path=TMP / "data.json")
    second = service.save(content={"a": 2}, artifact_format="json", path=TMP / "data.json")
    assert_true("first_path", Path(first.path).name == "data.json")
    assert_true("second_unique", Path(second.path).name.startswith("data_"))
    assert_equal("original_content", json.loads(Path(first.path).read_text(encoding="utf-8"))["a"], 1)


def test_csv_accepts_rows() -> None:
    reset_tmp()
    result = ArtifactSaveService().save(
        content=[{"name": "A", "price": 1}, {"name": "B", "price": 2}],
        artifact_format="csv",
        path=TMP,
        filename="items.csv",
    )
    text = Path(result.path).read_text(encoding="utf-8-sig")
    assert_true("csv_header", "name,price" in text)
    assert_true("csv_row", "A,1" in text)


def test_unknown_format_is_objective_error() -> None:
    reset_tmp()
    try:
        ArtifactSaveService().save(content="x", artifact_format="exe", path=TMP)
    except ArtifactSaveError as exc:
        assert_true("error_message", "Unsupported artifact format" in str(exc))
        return
    raise AssertionError("unknown format should fail")


def main() -> int:
    test_save_markdown_to_directory_writes_manifest()
    test_save_json_and_avoid_overwrite_by_default()
    test_csv_accepts_rows()
    test_unknown_format_is_objective_error()
    print("artifact_save_service_scenarios passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
