from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.query_intent import analyze_query  # noqa: E402


def assert_equal(name: str, actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"{name}: expected {expected!r}, got {actual!r}")


def assert_true(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name}: {detail}")


def test_malformed_concept_metadata_does_not_break_known_mod_intent() -> None:
    intent = analyze_query(
        "create mod 怎么玩",
        [
            {
                "aliases": "create mod",
                "canonical": "ignored",
                "primary_source": "createwiki",
                "tasks": 7,
            },
            {
                "aliases": ["create mod"],
                "canonical": "Create mod automation",
                "primary_source": "createwiki",
                "tasks": [
                    3,
                    ["createwiki", "bad"],
                    ("createwiki", "valid", "reason", "85"),
                    ("createwiki", "too many", "reason", 80, "extra"),
                ],
            },
        ],
    )

    assert_equal("domain", intent.domain, "known_mod")
    assert_equal("canonical", intent.entity, "Create mod automation")
    assert_equal("search_queries", intent.search_queries, ["valid"])
    assert_equal("sources", intent.preferred_sources, ["createwiki"])
    assert_equal("normalized_tasks", intent.concept["tasks"], [("createwiki", "valid", "reason", 85.0)])


def test_non_mapping_concepts_are_ignored() -> None:
    intent = analyze_query("create mod 怎么玩", [7, "create mod", None])  # type: ignore[list-item]

    assert_true("does_not_crash", intent.domain in {"project", "ambiguous"})
    assert_equal("no_concept", intent.concept, None)


if __name__ == "__main__":
    test_malformed_concept_metadata_does_not_break_known_mod_intent()
    test_non_mapping_concepts_are_ignored()
    print("query_intent_scenarios: ok")
