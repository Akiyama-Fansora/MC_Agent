from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.web_server import Job, _collaboration_dialog_for, _crawler_job_identity_haystack  # noqa: E402


def assert_equal(name: str, actual, expected) -> None:
    if actual != expected:
        raise AssertionError(f"{name}: expected {expected!r}, got {actual!r}")


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def test_malformed_planned_tasks_do_not_break_collaboration_dialog() -> None:
    job = Job(
        id="malformed-job",
        kind="crawler",
        title="Crawler job",
        result={
            "planned_tasks": 7,
            "plan": {"topic": "Utopia Journey", "coverage_goals": {"items": "bosses"}},
        },
    )
    dialog = _collaboration_dialog_for(
        "Fill Utopia Journey data",
        job,
        True,
        requested_by="mcagent",
        delivery_target="MCagent/RAG",
    )
    assert_true("dialog_is_list", isinstance(dialog, list))
    assert_true("dialog_has_reason", any(item.get("state") == "\u5224\u65ad" for item in dialog))
    assert_true("malformed_tasks_hidden", not any(item.get("state") == "\u89c4\u5212" for item in dialog))


def test_valid_planned_tasks_remain_visible_and_identity_search_is_tolerant() -> None:
    job = Job(
        id="valid-job",
        kind="crawler",
        title="Crawler job",
        result={
            "planned_tasks": [{"source": "web_discovery", "query": "Utopia Journey bosses"}],
            "plan": {"topic": "Utopia Journey", "coverage_goals": ["bosses"]},
        },
    )
    dialog = _collaboration_dialog_for(
        "Fill Utopia Journey data",
        job,
        True,
        requested_by="mcagent",
        delivery_target="MCagent/RAG",
    )
    assert_true("planned_task_visible", any(item.get("state") == "\u89c4\u5212" and "bosses" in item.get("text", "") for item in dialog))
    assert_equal(
        "identity_haystack_ignores_malformed_tasks",
        _crawler_job_identity_haystack({"result": {"planned_tasks": 9}}),
        "",
    )


if __name__ == "__main__":
    test_malformed_planned_tasks_do_not_break_collaboration_dialog()
    test_valid_planned_tasks_remain_visible_and_identity_search_is_tolerant()
    print("web_server_job_metadata_scenarios passed")
