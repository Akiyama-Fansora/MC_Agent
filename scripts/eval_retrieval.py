from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.config import load_config  # noqa: E402
from mcagent.retriever import Retriever  # noqa: E402


DEFAULT_CASES = ROOT / "data" / "eval" / "retrieval_qa.jsonl"


def _load_cases(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_number, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            case = json.loads(line)
            case.setdefault("id", f"case_{line_number}")
            case.setdefault("expected_any", [])
            cases.append(case)
    return cases


def _result_blob(result: Any) -> str:
    return "\n".join(
        [
            result.title,
            result.source_path,
            result.url or "",
            result.text[:3000],
        ]
    ).lower()


def _first_hit_rank(results: list[Any], expected_any: list[str]) -> int | None:
    expected = [item.lower() for item in expected_any if item]
    if not expected:
        return None
    for rank, result in enumerate(results, start=1):
        blob = _result_blob(result)
        if any(item in blob for item in expected):
            return rank
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate MCagent retrieval against a small local QA set.")
    parser.add_argument("--cases", default=str(DEFAULT_CASES), help="JSONL file with question and expected_any fields.")
    parser.add_argument("--config", default=None, help="Optional config.json path.")
    parser.add_argument("--top-k", type=int, default=10, help="How many retrieval results to inspect per question.")
    parser.add_argument("--fail-under", type=float, default=None, help="Exit 1 if recall@top_k is below this value.")
    parser.add_argument("--show-results", action="store_true", help="Print top result titles for every case.")
    args = parser.parse_args()

    config = load_config(args.config)
    retriever = Retriever(config)
    cases = _load_cases(Path(args.cases))
    if not cases:
        print("No eval cases found.")
        return 1

    hits_at_3 = 0
    hits_at_k = 0
    reciprocal_rank_total = 0.0
    rows: list[dict[str, Any]] = []
    for case in cases:
        results = retriever.search(str(case["question"]), top_k=args.top_k)
        hit_rank = _first_hit_rank(results, list(case["expected_any"]))
        if hit_rank is not None and hit_rank <= 3:
            hits_at_3 += 1
        if hit_rank is not None and hit_rank <= args.top_k:
            hits_at_k += 1
            reciprocal_rank_total += 1 / hit_rank
        rows.append(
            {
                "id": case["id"],
                "question": case["question"],
                "hit_rank": hit_rank,
                "top_titles": [result.title for result in results[:5]],
            }
        )

    total = len(cases)
    summary = {
        "cases": total,
        "recall@3": round(hits_at_3 / total, 4),
        f"recall@{args.top_k}": round(hits_at_k / total, 4),
        "mrr": round(reciprocal_rank_total / total, 4),
    }

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    for row in rows:
        mark = "OK" if row["hit_rank"] else "MISS"
        print(f"{mark} {row['id']} rank={row['hit_rank']} question={row['question']}")
        if args.show_results:
            for index, title in enumerate(row["top_titles"], start=1):
                print(f"  {index}. {title}")

    if args.fail_under is not None and summary[f"recall@{args.top_k}"] < args.fail_under:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
