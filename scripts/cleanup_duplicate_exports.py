from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any

from crawl_ledger import normalize_url


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_DIR = PROJECT_ROOT / "data" / "crawler_exports"
DEFAULT_REPORT_DIR = PROJECT_ROOT / "runtime" / "cleanup_reports"

SOURCE_PRIORITY = {
    "mcmod": 100,
    "fetch_url": 90,
    "playwright": 85,
    "web_discovery": 75,
    "modrinth_agent": 60,
    "followup": 55,
    "ftbwiki": 50,
    "createwiki": 50,
    "mediawiki": 40,
}

URL_PATTERNS = (
    re.compile(r"^- \*\*(?:URL|MC百科 URL|Modrinth URL):\*\*\s*(\S+)\s*$", re.M),
    re.compile(r"https?://[^\s)>\"]+", re.I),
)


def source_name(path: Path, source_dir: Path) -> str:
    try:
        return path.relative_to(source_dir).parts[0]
    except ValueError:
        return ""


def extract_url(markdown: str) -> str:
    for pattern in URL_PATTERNS:
        match = pattern.search(markdown)
        if match:
            return match.group(1 if pattern.groups else 0).strip().rstrip(".,")
    return ""


def score_file(path: Path, source_dir: Path) -> tuple[int, int, float]:
    source = source_name(path, source_dir)
    priority = SOURCE_PRIORITY.get(source, 10)
    try:
        stat = path.stat()
    except OSError:
        return priority, 0, 0.0
    return priority, stat.st_size, stat.st_mtime


def raw_html_candidates(markdown_path: Path) -> list[Path]:
    run_dir = markdown_path.parent
    raw_dir = run_dir / "raw_html"
    return [
        raw_dir / f"{markdown_path.stem}.html",
        raw_dir / f"{markdown_path.stem}.htm",
    ]


def safe_delete(path: Path, source_dir: Path) -> bool:
    try:
        resolved = path.resolve()
        root = source_dir.resolve()
    except OSError:
        return False
    if root not in resolved.parents and resolved != root:
        raise RuntimeError(f"Refusing to delete outside source dir: {resolved}")
    if not resolved.exists() or not resolved.is_file():
        return False
    resolved.unlink()
    return True


def build_duplicate_plan(source_dir: Path) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {}
    unreadable: list[str] = []
    for path in source_dir.rglob("*.md"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            unreadable.append(str(path))
            continue
        url = extract_url(text)
        canonical = normalize_url(url)
        if not canonical:
            continue
        groups.setdefault(canonical, []).append(
            {
                "path": str(path),
                "source": source_name(path, source_dir),
                "url": url,
                "chars": len(text),
                "bytes": path.stat().st_size,
            }
        )

    duplicate_groups: list[dict[str, Any]] = []
    delete_paths: list[str] = []
    for canonical, files in groups.items():
        if len(files) < 2:
            continue
        sorted_files = sorted(
            files,
            key=lambda item: score_file(Path(item["path"]), source_dir),
            reverse=True,
        )
        keep = sorted_files[0]
        duplicates = sorted_files[1:]
        duplicate_groups.append({"canonical_url": canonical, "keep": keep, "delete": duplicates})
        delete_paths.extend(item["path"] for item in duplicates)

    return {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source_dir": str(source_dir),
        "rule": "same_normalized_url_only",
        "duplicate_groups": duplicate_groups,
        "delete_paths": delete_paths,
        "unreadable": unreadable,
        "summary": {
            "duplicate_groups": len(duplicate_groups),
            "markdown_to_delete": len(delete_paths),
        },
    }


def cleanup_empty_dirs(source_dir: Path) -> int:
    removed = 0
    for path in sorted(source_dir.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if not path.is_dir():
            continue
        try:
            if path == source_dir or any(path.iterdir()):
                continue
            resolved = path.resolve()
            root = source_dir.resolve()
            if root not in resolved.parents:
                continue
            path.rmdir()
            removed += 1
        except OSError:
            continue
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description="Safely delete duplicate crawler exports by same normalized URL.")
    parser.add_argument("--source-dir", default=str(DEFAULT_SOURCE_DIR))
    parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR))
    parser.add_argument("--apply", action="store_true", help="Actually delete duplicate markdown/raw_html files.")
    args = parser.parse_args()

    source_dir = Path(args.source_dir).resolve()
    report_dir = Path(args.report_dir).resolve()
    if not source_dir.exists():
        raise SystemExit(f"source dir does not exist: {source_dir}")
    plan = build_duplicate_plan(source_dir)

    deleted_markdown: list[str] = []
    deleted_raw_html: list[str] = []
    if args.apply:
        for raw_path in plan["delete_paths"]:
            markdown_path = Path(raw_path)
            if safe_delete(markdown_path, source_dir):
                deleted_markdown.append(str(markdown_path))
            for raw_candidate in raw_html_candidates(markdown_path):
                if safe_delete(raw_candidate, source_dir):
                    deleted_raw_html.append(str(raw_candidate))
        plan["deleted"] = {
            "markdown": len(deleted_markdown),
            "raw_html": len(deleted_raw_html),
            "markdown_paths": deleted_markdown,
            "raw_html_paths": deleted_raw_html,
            "empty_dirs_removed": cleanup_empty_dirs(source_dir),
        }
    else:
        plan["deleted"] = {"markdown": 0, "raw_html": 0, "empty_dirs_removed": 0}

    report_dir.mkdir(parents=True, exist_ok=True)
    suffix = "apply" if args.apply else "dry_run"
    report_path = report_dir / f"duplicate_cleanup_{suffix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    report_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Report: {report_path}")
    print(json.dumps(plan["summary"], ensure_ascii=False))
    if args.apply:
        print(json.dumps(plan["deleted"], ensure_ascii=False)[:2000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
