from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import sys
import time
from typing import Any
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fetch_mcmod_seed import DEFAULT_USER_AGENT, bootstrap_cookie, follow_cookie_gate, request_text, slugify
from mcagent.cleaners import html_to_text, markdown_title, normalize_text


DEFAULT_DEST = PROJECT_ROOT / "data" / "crawler_exports"


def _now_slug() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def _load_records(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    records = data.get("records") if isinstance(data, dict) else None
    if isinstance(records, list):
        return [item for item in records if isinstance(item, dict)]
    raise ValueError(f"Unsupported URL list JSON: {path}")


def _allowed_mcmod_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.netloc.lower() not in {"www.mcmod.cn", "mcmod.cn"}:
        return False
    return bool(re.match(r"^/(class|item|post|modpack|course)/[^/?#]+\.html$", parsed.path))


def _metadata_lines(record: dict[str, Any], final_url: str) -> list[str]:
    lines = ["## Metadata", "", f"- **MC百科 URL:** {final_url}"]
    for key in ("name", "title", "version", "category", "category_id", "mcmod_id"):
        value = record.get(key)
        if value not in (None, ""):
            lines.append(f"- **{key}:** {value}")
    return lines


def fetch_urls(
    urls_json: Path,
    dest_root: Path,
    limit: int,
    offset: int,
    delay: float,
    user_agent: str,
) -> dict[str, Any]:
    records = _load_records(urls_json)
    if offset > 0:
        records = records[offset:]
    run_dir = dest_root / "mcmod_url_batch" / _now_slug()
    raw_dir = run_dir / "raw_html"
    raw_dir.mkdir(parents=True, exist_ok=True)

    cookie = bootstrap_cookie(user_agent)
    manifest: dict[str, Any] = {
        "manifest_type": "mcmod_url_batch_export",
        "source": "mcmod_urls",
        "input": str(urls_json),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "export_dir": str(run_dir),
        "records": [],
        "skipped": [],
        "errors": [],
    }

    seen_urls: set[str] = set()
    selected = records[: limit if limit > 0 else None]
    for index, record in enumerate(selected, start=1):
        url = str(record.get("url") or "").strip()
        if not url:
            manifest["skipped"].append({"index": index, "reason": "missing_url", "record": record})
            continue
        if url in seen_urls:
            manifest["skipped"].append({"index": index, "url": url, "reason": "duplicate_input"})
            continue
        seen_urls.add(url)
        if not _allowed_mcmod_url(url):
            manifest["skipped"].append({"index": index, "url": url, "reason": "unsupported_url"})
            continue
        try:
            html, content_type, status, final_url = request_text(url, user_agent=user_agent, cookie=cookie, retries=2)
            html, _content_type2, _status2, final_url = follow_cookie_gate(html, final_url, user_agent, cookie)
            text, html_title = html_to_text(html)
            text = normalize_text(text)
            if not text:
                manifest["skipped"].append({"index": index, "url": url, "reason": "empty_text"})
                continue
            title = str(record.get("name") or record.get("title") or html_title or markdown_title(text, "mcmod_page"))
            safe = slugify(f"{index:03d}_{title}", "mcmod_url")
            raw_path = raw_dir / f"{safe}.html"
            md_path = run_dir / f"{safe}.md"
            raw_path.write_text(html, encoding="utf-8")
            body = "\n".join(
                [
                    f"# {title}",
                    "",
                    *_metadata_lines(record, final_url),
                    f"- **raw_html_path:** {raw_path}",
                    f"- **http_status:** {status}",
                    f"- **content_type:** {content_type}",
                    "",
                    "## Content",
                    "",
                    text,
                    "",
                ]
            )
            md_path.write_text(body, encoding="utf-8")
            manifest["records"].append(
                {
                    "index": index,
                    "title": title,
                    "url": final_url,
                    "path": str(md_path),
                    "raw_html_path": str(raw_path),
                    "chars": len(text),
                    "metadata": {key: record.get(key) for key in ("version", "category", "category_id", "mcmod_id") if record.get(key) not in (None, "")},
                }
            )
        except Exception as exc:  # noqa: BLE001
            manifest["errors"].append({"index": index, "url": url, "error": f"{type(exc).__name__}: {exc}"})
        if delay > 0 and index < len(selected):
            time.sleep(delay)

    (run_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fetch known MC百科 URLs into Markdown + raw HTML + manifest.")
    parser.add_argument("--urls-json", required=True, help="JSON file with records containing URL fields.")
    parser.add_argument("--dest", default=str(DEFAULT_DEST), help="Destination root. Defaults to data/crawler_exports.")
    parser.add_argument("--limit", type=int, default=0, help="Maximum URLs to fetch. 0 means all.")
    parser.add_argument("--offset", type=int, default=0, help="Skip this many records from the input list.")
    parser.add_argument("--delay", type=float, default=0.35, help="Delay between requests.")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest = fetch_urls(
        urls_json=Path(args.urls_json).resolve(),
        dest_root=Path(args.dest).resolve(),
        limit=args.limit,
        offset=max(0, args.offset),
        delay=args.delay,
        user_agent=args.user_agent,
    )
    print(f"Exported to: {manifest['export_dir']}")
    print(f"Markdown files: {len(manifest['records'])}")
    print(f"Skipped: {len(manifest['skipped'])}")
    print(f"Errors: {len(manifest['errors'])}")
    return 0 if not manifest["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
