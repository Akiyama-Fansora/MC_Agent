from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.audit_dual_agent_architecture import audit  # noqa: E402


def assert_true(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name}: {detail}")


def test_architecture_audit_reports_two_agent_shape_and_migration_warning() -> None:
    report = audit(ROOT)
    summary = report["summary"]
    checks = {item["id"]: item for item in report["checks"]}
    assert_true("two_agent_shape", summary["is_two_agent_shaped"], str(report))
    assert_true("message_triplet", checks["agent_message_triplet"]["status"] == "pass", str(checks["agent_message_triplet"]))
    assert_true("conversation_route", checks["conversation_routes_by_to_agent"]["status"] == "pass", str(checks["conversation_routes_by_to_agent"]))
    assert_true("mcagent_boundary", checks["mcagent_blocks_public_web_tools"]["status"] == "pass", str(checks["mcagent_blocks_public_web_tools"]))
    assert_true("crawler_split", checks["crawler_general_and_domain_tools_split"]["status"] == "pass", str(checks["crawler_general_and_domain_tools_split"]))
    assert_true("explicit_legacy_adapter", checks["explicit_legacy_runtime_adapter"]["status"] == "pass", str(checks["explicit_legacy_runtime_adapter"]))
    assert_true("explicit_runtime_request_contracts", checks["explicit_runtime_request_contracts"]["status"] == "pass", str(checks["explicit_runtime_request_contracts"]))
    assert_true("explicit_route_input_contracts", checks["explicit_route_input_contracts"]["status"] == "pass", str(checks["explicit_route_input_contracts"]))
    assert_true("explicit_message_preflight_contracts", checks["explicit_message_preflight_contracts"]["status"] == "pass", str(checks["explicit_message_preflight_contracts"]))
    assert_true("legacy_warning_visible", checks["legacy_runtime_coupling_visible"]["status"] == "warn", str(checks["legacy_runtime_coupling_visible"]))


if __name__ == "__main__":
    test_architecture_audit_reports_two_agent_shape_and_migration_warning()
    print("dual_agent_architecture_audit_scenarios passed")
