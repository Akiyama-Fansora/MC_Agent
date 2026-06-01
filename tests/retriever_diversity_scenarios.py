from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.retriever import _select_diverse_ranked_items  # noqa: E402


def assert_equal(name: str, actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"{name}: expected {expected!r}, got {actual!r}")


def assert_true(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name}: {detail}")


def row(document_id: int, source_path: str, title: str = "", text: str = "") -> dict[str, object]:
    return {"document_id": document_id, "source_path": source_path, "title": title, "text": text}


def test_project_retrieval_keeps_source_diversity_after_high_score_long_document() -> None:
    rows_by_id: dict[int, dict[str, object]] = {}
    reranked: list[tuple[float, int]] = []
    for chunk_id in range(1, 9):
        rows_by_id[chunk_id] = row(100, r"D:\magic\MC_Agent\data\crawler_exports\mcmod\run\accepted_by_crawler\create.html")
        reranked.append((100.0 - chunk_id, chunk_id))
    rows_by_id[20] = row(200, r"D:\magic\MC_Agent\data\crawler_exports\modrinth_agent\run\accepted_by_crawler\mod_create.md")
    rows_by_id[21] = row(201, r"D:\magic\MC_Agent\data\crawler_exports\createwiki\run\create_Water-Wheel.md")
    rows_by_id[22] = row(202, r"D:\magic\MC_Agent\data\crawler_exports\web_discovery\run\accepted_by_crawler\create_readme.md")
    reranked.extend([(91.0, 20), (90.0, 21), (89.0, 22)])

    selected = _select_diverse_ranked_items(reranked, rows_by_id, 6, diversify_sources=True)
    selected_ids = [chunk_id for _score, chunk_id in selected]

    assert_equal("limit", len(selected_ids), 6)
    assert_true("keeps_best_mcmod", {1, 2}.issubset(set(selected_ids)), str(selected_ids))
    assert_true("includes_modrinth", 20 in selected_ids, str(selected_ids))
    assert_true("includes_createwiki", 21 in selected_ids, str(selected_ids))
    assert_true("includes_web_discovery", 22 in selected_ids, str(selected_ids))
    assert_true("does_not_allow_long_doc_to_fill_all_slots", len([item for item in selected_ids if item < 9]) <= 3, str(selected_ids))


def test_non_project_retrieval_preserves_plain_score_order() -> None:
    rows_by_id = {
        1: row(100, "a.md"),
        2: row(100, "a.md"),
        3: row(200, "b.md"),
    }
    reranked = [(3.0, 1), (2.0, 2), (1.0, 3)]

    selected = _select_diverse_ranked_items(reranked, rows_by_id, 2, diversify_sources=False)

    assert_equal("plain_order", selected, [(3.0, 1), (2.0, 2)])


def test_project_diversity_does_not_fill_with_off_topic_pack_internals() -> None:
    intent = SimpleNamespace(
        domain="known_mod",
        entity="Create mod automation",
        keywords=["Create mod automation", "rotational", "power", "stress", "kinetics", "automation"],
        search_queries=["Create mechanical power"],
        concept={"aliases": ["create mod", "mechanical power"]},
    )
    rows_by_id = {
        1: row(
            100,
            r"D:\magic\MC_Agent\data\crawler_exports\createwiki\run\create_Shaft.md",
            "Shaft",
            "Create mod mechanical power rotation stress units shaft cogwheel kinetics.",
        ),
        2: row(
            101,
            r"D:\magic\MC_Agent\data\crawler_exports\modrinth_agent\run\accepted_by_crawler\mod_create.md",
            "Create - Modrinth",
            "Create mod supports loaders and versions for mechanical automation.",
        ),
        3: row(
            102,
            r"D:\magic\MC_Agent\data\crawler_exports\mcmod\run\accepted_by_crawler\create.html",
            "Create / Mechanical Power",
            "Rotational power stress kinetics automation.",
        ),
        4: row(
            200,
            r"D:\magic\MC_Agent\data\crawler_exports\manual_research\MinePIxelWuTuoBang\pack_internal\BetterDesertTemples.md",
            "Better Desert Temples config",
            "For example, on Forge 1.18.2 the file is betterdeserttemples-forge.toml.",
        ),
    }
    reranked = [(10.0, 1), (9.5, 2), (9.0, 3), (8.8, 4)]

    selected = _select_diverse_ranked_items(
        reranked,
        rows_by_id,
        6,
        diversify_sources=True,
        query="Create mechanical power stress kinetics beginner automation supported loaders versions",
        intent=intent,
    )
    selected_ids = [chunk_id for _score, chunk_id in selected]

    assert_equal("selected_ids", selected_ids, [1, 2, 3])
    assert_true("off_topic_pack_internal_excluded", 4 not in selected_ids, str(selected_ids))


if __name__ == "__main__":
    test_project_retrieval_keeps_source_diversity_after_high_score_long_document()
    test_non_project_retrieval_preserves_plain_score_order()
    test_project_diversity_does_not_fill_with_off_topic_pack_internals()
    print("retriever_diversity_scenarios: ok")
