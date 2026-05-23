from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = ROOT / "data" / "crawler_exports"


TEXT_SUFFIXES = {".md", ".txt", ".json", ".jsonl", ".csv", ".html", ".htm", ".py", ".js", ".ts", ".css", ".toml", ".yaml", ".yml"}


def now_slug() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def query_terms(query: str) -> list[str]:
    terms = re.findall(r"[A-Za-z0-9_+-]{2,}|[\u4e00-\u9fff]{2,}", query.lower())
    return list(dict.fromkeys(terms))


def search_files(root: Path, query: str, output_root: Path, *, max_files: int, max_chars: int) -> dict[str, object]:
    run_dir = output_root / "local_file_search" / now_slug()
    run_dir.mkdir(parents=True, exist_ok=True)
    created_at = datetime.now().isoformat(timespec="seconds")
    terms = query_terms(query)
    records: list[dict[str, object]] = []
    skipped: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    try:
        resolved_root = root.expanduser().resolve()
        if not resolved_root.exists():
            raise FileNotFoundError(str(resolved_root))
        candidates = [resolved_root] if resolved_root.is_file() else list(resolved_root.rglob("*"))
        for path in candidates:
            if len(records) >= max_files:
                break
            if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                skipped.append({"path": str(path), "reason": f"read_error: {exc}"})
                continue
            haystack = f"{path}\n{text[:max_chars]}".lower()
            score = sum(1 for term in terms if term in haystack)
            if terms and score == 0:
                continue
            snippet = text[:max_chars]
            records.append({"title": path.name, "path": str(path), "score": score, "chars": len(text), "snippet": snippet[:2000]})
    except Exception as exc:  # noqa: BLE001
        errors.append({"path": str(root), "error": f"{type(exc).__name__}: {exc}"})

    report_path = run_dir / "local_file_search_report.md"
    lines = [
        f"# Local File Search: {query}",
        "",
        "<!-- source: local_file_search -->",
        "",
        "## Metadata",
        "",
        f"- **Root:** {root}",
        f"- **Query:** {query}",
        f"- **Searched at:** {created_at}",
        "",
        "## Matches",
        "",
    ]
    for item in records:
        lines.extend([f"### {item['title']}", "", f"- **Path:** {item['path']}", f"- **Score:** {item['score']}", "", "```text", str(item["snippet"]), "```", ""])
    report_path.write_text("\n".join(lines), encoding="utf-8")

    manifest = {
        "manifest_type": "local_file_search_export",
        "provider": "local_file_search",
        "created_at": created_at,
        "export_dir": str(run_dir),
        "query": query,
        "records": [{"title": item["title"], "path": str(report_path), "source_path": item["path"], "score": item["score"], "chars": item["chars"]} for item in records],
        "candidates": records,
        "skipped": skipped,
        "errors": errors,
        "status": "ok" if records else "empty",
        "failure_reason": "; ".join(item["error"] for item in errors),
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Search local text files and expose matches as crawler artifacts.")
    parser.add_argument("--path", required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--max-files", type=int, default=25)
    parser.add_argument("--max-chars", type=int, default=12000)
    args = parser.parse_args()
    manifest = search_files(Path(args.path), args.query, Path(args.output_root), max_files=args.max_files, max_chars=args.max_chars)
    print(json.dumps({"export_dir": manifest["export_dir"], "records": len(manifest["records"]), "errors": len(manifest["errors"])}, ensure_ascii=False, indent=2))
    print(f"Exported to: {manifest['export_dir']}")
    return 0 if manifest["records"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
