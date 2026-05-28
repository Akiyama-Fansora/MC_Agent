from __future__ import annotations

from pathlib import Path
import json
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.crawler_reflection_service import CrawlerReflectionSnapshotService  # noqa: E402


def assert_equal(name: str, actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"{name}: expected {expected!r}, got {actual!r}")


def assert_true(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name}: {detail}")


def test_snapshot_counts_observations_and_pressure() -> None:
    snapshot = CrawlerReflectionSnapshotService().build(
        plan={
            "topic": "乌托邦探险之旅",
            "delivery_target": "MCagent/RAG",
            "coverage_goals": ["完整模组列表"],
            "success_criteria": ["可引用"],
            "sources": ["mcmod", "playwright"],
        },
        task_results=[
            {"source": "mcmod", "query": "乌托邦", "returncode": 0, "manifest_stats": {"records": 0}, "empty_result": True},
            {"source": "playwright", "query": "乌托邦 玩法", "returncode": 1, "output": "HTTP 429 quota exceeded"},
        ],
        pending_tasks=[{"source": "playwright", "query": "乌托邦探险之旅", "reason": "render project page"}],
    )

    assert_equal("topic", snapshot["plan"]["topic"], "乌托邦探险之旅")
    assert_equal("recent_count", len(snapshot["recent_results"]), 2)
    assert_equal("pending_count", len(snapshot["pending_tasks"]), 1)
    assert_equal("empty_count", snapshot["observation_statuses"].get("empty"), 1)
    assert_equal("quota_count", snapshot["observation_statuses"].get("quota_limited"), 1)
    assert_equal("pressure", snapshot["pressure"], "quota_limited_change_source")
    assert_true("retryable_count", int(snapshot["retryable_recent_results"]) >= 1)


def test_snapshot_detects_poor_yield_replan_pressure() -> None:
    snapshot = CrawlerReflectionSnapshotService().build(
        plan={"topic": "落幕曲"},
        task_results=[
            {"source": "fetch_url", "returncode": 0, "empty_result": True, "manifest_stats": {"records": 0}},
            {"source": "web_discovery", "returncode": 0, "off_topic_result": True, "manifest_stats": {"records": 1}},
            {"source": "web_discovery", "returncode": 0, "empty_result": True, "manifest_stats": {"records": 0}},
        ],
        pending_tasks=[{"source": "mcmod", "query": "落幕曲 Boss"}],
    )

    assert_equal("pressure", snapshot["pressure"], "poor_yield_replan_queries_or_sources")
    assert_equal("pending_source", snapshot["pending_tasks"][0]["source"], "mcmod")


def test_snapshot_surfaces_crawler_review_actions() -> None:
    snapshot = CrawlerReflectionSnapshotService().build(
        plan={"topic": "Utopian Journey"},
        task_results=[
            {
                "source": "playwright",
                "query": "https://modrinth.com/project/utopia-exploration-modpack",
                "returncode": 0,
                "manifest_stats": {"records": 1},
                "topic_validation": {
                    "matched": False,
                    "reason": "not_found",
                    "cleanup_action": "retry_other_source",
                    "next_action": "Search community mirrors instead.",
                    "rejected_examples": [{"title": "Modrinth", "url": "https://modrinth.com/project/utopia-exploration-modpack"}],
                },
                "off_topic_result": True,
            }
        ],
        pending_tasks=[],
    )
    recent = snapshot["recent_results"][0]
    assert_equal("review_action", recent["crawler_review_action"], "retry_other_source")
    assert_equal("review_next", recent["crawler_review_next_action"], "Search community mirrors instead.")
    assert_equal("rejected_title", recent["rejected_examples"][0]["title"], "Modrinth")


def test_snapshot_surfaces_rejected_duplicate_review() -> None:
    snapshot = CrawlerReflectionSnapshotService().build(
        plan={"topic": "Utopian Journey"},
        task_results=[
            {
                "source": "playwright",
                "query": "Utopian Journey Modrinth",
                "returncode": 0,
                "manifest_stats": {"records": 0, "skipped": 1},
                "existing_evidence_review": {
                    "matched": False,
                    "reason": "not_found",
                    "cleanup_action": "retry_other_source",
                    "next_action": "Do not reuse the duplicate 404 page; search exact aliases elsewhere.",
                },
                "empty_result": True,
            }
        ],
        pending_tasks=[],
    )
    recent = snapshot["recent_results"][0]
    assert_equal("duplicate_reason", recent["duplicate_review_reason"], "not_found")
    assert_equal("duplicate_action", recent["duplicate_review_action"], "retry_other_source")
    assert_equal("duplicate_next", recent["duplicate_review_next_action"], "Do not reuse the duplicate 404 page; search exact aliases elsewhere.")


def test_snapshot_marks_unreviewed_records() -> None:
    snapshot = CrawlerReflectionSnapshotService().build(
        plan={"topic": "Utopian Journey"},
        task_results=[
            {
                "source": "browser_collect",
                "query": "Utopian Journey",
                "returncode": 0,
                "manifest_stats": {"records": 4},
                "records_pending_review": True,
            }
        ],
        pending_tasks=[],
    )
    recent = snapshot["recent_results"][0]
    assert_equal("pending_review", recent["records_pending_review"], True)
    assert_equal("observation_status", recent["observation_status"], "records_pending_review")


def test_snapshot_surfaces_manifest_candidate_preview() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        manifest_path = Path(tmp) / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "query": "Utopian Journey",
                    "records": [
                        {
                            "title": "Page not found · GitHub",
                            "url": "https://github.com/Utopia-Exploration/Modpack",
                            "path": str(Path(tmp) / "page.md"),
                        }
                    ],
                    "candidates": [{"title": "BBSMC Utopian Journey", "url": "https://bbsmc.net/modpack/utopia-journey"}],
                    "skipped": [{"title": "Video tutorial", "url": "https://example.test/video", "reason": "skip_host_or_non_text"}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        snapshot = CrawlerReflectionSnapshotService().build(
            plan={"topic": "Utopian Journey"},
            task_results=[
                {
                    "source": "playwright",
                    "query": "Utopian Journey",
                    "returncode": 0,
                    "manifest_stats": {"records": 1, "skipped": 1, "manifest_path": str(manifest_path)},
                    "records_pending_review": True,
                }
            ],
            pending_tasks=[],
        )
    preview = snapshot["recent_results"][0]["manifest_preview"]
    assert_equal("record_title", preview["records"][0]["title"], "Page not found · GitHub")
    assert_equal("candidate_url", preview["candidates"][0]["url"], "https://bbsmc.net/modpack/utopia-journey")
    assert_equal("skipped_reason", preview["skipped"][0]["reason"], "skip_host_or_non_text")


if __name__ == "__main__":
    test_snapshot_counts_observations_and_pressure()
    test_snapshot_detects_poor_yield_replan_pressure()
    test_snapshot_surfaces_crawler_review_actions()
    test_snapshot_surfaces_rejected_duplicate_review()
    test_snapshot_marks_unreviewed_records()
    test_snapshot_surfaces_manifest_candidate_preview()
    print("crawler_reflection_service_scenarios: ok")

