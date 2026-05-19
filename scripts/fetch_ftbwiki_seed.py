from __future__ import annotations

import argparse
from datetime import datetime
from html import unescape
import json
from pathlib import Path
import re
import sys
import time
from typing import Any
from urllib.parse import urlencode
import urllib.error
import urllib.request


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from crawl_ledger import append_ledger, content_hash, ledger_record, load_ledger, make_key
from mcagent.cleaners import normalize_text


API_ENDPOINT = "https://ftbwiki.org/api.php"
DEFAULT_USER_AGENT = "MC_Agent/0.1 (FTB Wiki crawler; D:/magic/MC_Agent)"


def slugify(value: str, fallback: str = "page") -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-._")
    return cleaned[:90] or fallback


def request_json(params: dict[str, Any], user_agent: str, retries: int = 2) -> dict[str, Any]:
    request = urllib.request.Request(
        API_ENDPOINT + "?" + urlencode(params),
        headers={"Accept": "application/json", "User-Agent": user_agent},
    )
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8", errors="replace"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
        if attempt < retries:
            time.sleep(0.8 * (attempt + 1))
    raise RuntimeError(f"FTB Wiki API request failed: {last_error}")


def search_titles(query: str, limit: int, user_agent: str) -> list[str]:
    if not query:
        return []
    data = request_json(
        {
            "action": "query",
            "format": "json",
            "list": "search",
            "srsearch": query,
            "srlimit": max(1, min(limit, 50)),
        },
        user_agent,
    )
    rows = data.get("query", {}).get("search", [])
    return [str(row.get("title")) for row in rows if isinstance(row, dict) and row.get("title")]


def seed_titles_for_query(query: str) -> list[str]:
    lowered = query.lower()
    titles: list[str] = []
    if "twilight forest" in lowered or "暮色" in query:
        titles.extend(
            [
                "Twilight Forest",
                "Twilight Forest Landmarks",
                "Naga",
                "Lich",
                "Hydra",
                "Ur-Ghast",
                "Alpha Yeti",
                "Snow Queen",
                "Minoshroom",
                "Knight Phantom",
            ]
        )
    if "boss" in lowered or "bosses" in lowered:
        titles.extend(["Boss", "Twilight Forest Landmarks"])
    return titles


def unique_titles(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = value.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(value.strip())
    return result


def fetch_pages(titles: list[str], user_agent: str, batch_size: int = 8) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    for start in range(0, len(titles), batch_size):
        batch = titles[start : start + batch_size]
        if not batch:
            continue
        data = request_json(
            {
                "action": "query",
                "format": "json",
                "prop": "revisions|info",
                "rvprop": "content",
                "rvslots": "main",
                "inprop": "url",
                "titles": "|".join(batch),
                "redirects": "1",
            },
            user_agent,
        )
        raw_pages = data.get("query", {}).get("pages", {})
        if isinstance(raw_pages, dict):
            pages.extend(row for row in raw_pages.values() if isinstance(row, dict))
        time.sleep(0.15)
    return pages


def strip_templates(text: str) -> str:
    # Lightweight wikitext cleanup: enough to make FTB Wiki pages useful for RAG.
    previous = None
    while previous != text:
        previous = text
        text = re.sub(r"\{\{[^{}]*\}\}", " ", text, flags=re.S)
    return text


def wikitext_to_text(text: str) -> str:
    text = strip_templates(text)
    text = re.sub(r"<ref[^>]*>.*?</ref>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\[\[File:[^\]]+\]\]", " ", text, flags=re.I)
    text = re.sub(r"\[\[Image:[^\]]+\]\]", " ", text, flags=re.I)
    text = re.sub(r"\[\[([^|\]]+)\|([^\]]+)\]\]", r"\2", text)
    text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"\[https?://[^\s\]]+\s+([^\]]+)\]", r"\1", text)
    text = re.sub(r"'{2,}", "", text)
    text = re.sub(r"^=+\s*(.*?)\s*=+$", r"\n## \1\n", text, flags=re.M)
    text = re.sub(r"^\|.*$", " ", text, flags=re.M)
    text = re.sub(r"^\!.*$", " ", text, flags=re.M)
    text = re.sub(r"^\{\|.*?$|^\|\}.*?$", " ", text, flags=re.M)
    return normalize_text(unescape(text))


def page_content(page: dict[str, Any]) -> str:
    revisions = page.get("revisions") or []
    if not revisions or not isinstance(revisions[0], dict):
        return ""
    revision = revisions[0]
    slots = revision.get("slots")
    if isinstance(slots, dict) and isinstance(slots.get("main"), dict):
        return str(slots["main"].get("*") or slots["main"].get("content") or "")
    return str(revision.get("*") or "")


def page_to_markdown(page: dict[str, Any], fetched_at: str, query: str, content: str) -> str:
    title = str(page.get("title") or "Untitled")
    url = str(page.get("fullurl") or f"https://ftbwiki.org/{title.replace(' ', '_')}")
    lines = [
        f"# {title}",
        "",
        "<!-- source: ftbwiki_mediawiki_api -->",
        "",
        "## Metadata",
        "",
        f"- **FTB Wiki URL:** {url}",
        f"- **Page ID:** {page.get('pageid', '')}",
        f"- **Fetched at:** {fetched_at}",
    ]
    if query:
        lines.append(f"- **MCagent query:** {query}")
    lines.extend(["", "## Content", "", content])
    return "\n".join(lines).strip() + "\n"


def fetch_seed(dest_root: Path, query: str, search_limit: int, user_agent: str, force: bool = False) -> dict[str, Any]:
    run_dir = dest_root / "ftbwiki" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    run_dir.mkdir(parents=True, exist_ok=True)
    fetched_at = datetime.now().isoformat(timespec="seconds")
    ledger = load_ledger()
    errors: list[dict[str, str]] = []

    titles = unique_titles(seed_titles_for_query(query) + search_titles(query, search_limit, user_agent))
    try:
        pages = fetch_pages(titles, user_agent)
    except Exception as exc:  # noqa: BLE001
        pages = []
        errors.append({"stage": "fetch_pages", "error": str(exc)})

    records: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for page in pages:
        title = str(page.get("title") or "")
        if not title or page.get("missing") is not None:
            continue
        content = wikitext_to_text(page_content(page))
        if len(content) < 120:
            continue
        item_id = str(page.get("pageid") or title)
        url = str(page.get("fullurl") or f"https://ftbwiki.org/{title.replace(' ', '_')}")
        key = make_key("ftbwiki", item_id)
        digest = content_hash(content)
        previous = ledger.get(key)
        if previous and previous.get("content_hash") == digest and not force:
            skipped.append({"title": title, "pageid": page.get("pageid"), "url": url, "reason": "unchanged", "previous_path": previous.get("path", "")})
            append_ledger(
                ledger_record(
                    source="ftbwiki",
                    item_id=item_id,
                    title=title,
                    url=url,
                    text=content,
                    path=str(previous.get("path", "")),
                    query=query,
                    status="skipped_unchanged",
                    previous=previous,
                )
            )
            continue
        path = run_dir / f"ftb_{slugify(title)}.md"
        markdown = page_to_markdown(page, fetched_at, query, content)
        path.write_text(markdown, encoding="utf-8")
        append_ledger(
            ledger_record(
                source="ftbwiki",
                item_id=item_id,
                title=title,
                url=url,
                text=content,
                path=str(path),
                query=query,
                status="updated" if previous else "new",
                previous=previous,
            )
        )
        records.append({"title": title, "pageid": page.get("pageid"), "url": url, "path": str(path), "chars": len(content)})

    manifest = {
        "manifest_type": "ftbwiki_seed_export",
        "created_at": fetched_at,
        "api_endpoint": API_ENDPOINT,
        "export_dir": str(run_dir),
        "query": query,
        "titles_requested": titles,
        "records": records,
        "skipped": skipped,
        "errors": errors,
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fetch FTB Wiki MediaWiki pages as Markdown for MCagent RAG.")
    parser.add_argument("--dest", default=str(PROJECT_ROOT / "data" / "crawler_exports"))
    parser.add_argument("--query", default="")
    parser.add_argument("--search-limit", type=int, default=12)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    manifest = fetch_seed(
        dest_root=Path(args.dest).resolve(),
        query=args.query.strip(),
        search_limit=max(1, min(args.search_limit, 50)),
        user_agent=args.user_agent,
        force=args.force,
    )
    print(f"Exported to: {manifest['export_dir']}")
    print(f"Markdown files: {len(manifest['records'])}")
    print(f"Skipped unchanged: {len(manifest['skipped'])}")
    print(f"Errors: {len(manifest['errors'])}")
    return 0 if not manifest["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
