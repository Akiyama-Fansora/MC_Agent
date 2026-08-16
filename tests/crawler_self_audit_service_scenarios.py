from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcagent.crawler_self_audit_service import CrawlerSelfAuditService  # noqa: E402


def assert_equal(name: str, actual, expected) -> None:
    if actual != expected:
        raise AssertionError(f"{name}: expected={expected!r}, actual={actual!r}")


def test_invalid_manifest_counts_do_not_become_visible_evidence() -> None:
    audit = CrawlerSelfAuditService().build(
        [
            {
                "source": "fetch_url",
                "query": "https://example.test/project",
                "manifest_stats": {
                    "records": True,
                    "usable_records": -3,
                    "empty_records": 2.5,
                    "record_bytes": "-7",
                    "skipped": "4",
                    "errors": "4.8",
                },
                "observation": {"status": "ok", "summary": "tool returned malformed counts"},
            }
        ]
    )
    entry = audit["accepted_sources"][0]
    assert_equal("boolean_records_rejected", entry["records"], 0)
    assert_equal("negative_usable_records_rejected", entry["usable_records"], 0)
    assert_equal("fractional_empty_records_rejected", entry["empty_records"], 0)
    assert_equal("negative_record_bytes_rejected", entry["record_bytes"], 0)
    assert_equal("integer_string_skipped_kept", entry["skipped"], 4)
    assert_equal("fractional_string_errors_rejected", entry["errors"], 0)
    assert_equal("objective_records_normalized", entry["objective_evidence"]["records"], 0)
    assert_equal("objective_usable_records_normalized", entry["objective_evidence"]["usable_records"], 0)
    assert_equal("objective_record_bytes_normalized", entry["objective_evidence"]["record_bytes"], 0)


if __name__ == "__main__":
    test_invalid_manifest_counts_do_not_become_visible_evidence()
    print("crawler_self_audit_service_scenarios passed")
