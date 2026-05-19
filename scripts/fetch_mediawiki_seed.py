from __future__ import annotations

import argparse
from datetime import datetime
import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
import http.client
import urllib.error
import urllib.request

from crawl_ledger import append_ledger, content_hash, ledger_record, load_ledger, make_key


PROJECT_ROOT = Path(__file__).resolve().parents[1]
API_ENDPOINT = "https://minecraft.wiki/api.php"
DEFAULT_USER_AGENT = "MC_Agent/0.1 (local RAG seed; D:/magic/MC_Agent)"

CORE_TITLES = [
    "Minecraft",
    "Gameplay",
    "Survival",
    "Creative",
    "Adventure",
    "Hardcore",
    "Spectator",
    "Crafting",
    "Smelting",
    "Enchanting",
    "Brewing",
    "Trading",
    "Redstone circuits",
    "Command",
    "Difficulty",
    "Advancement",
    "Biome",
    "Dimension",
    "The Nether",
    "The End",
    "Mob",
    "Villager",
    "Structure",
    "Resource pack",
    "Data pack",
    "Multiplayer",
    "Server",
]


def slugify(value: str, fallback: str = "page") -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-._")
    return cleaned[:90] or fallback


def request_json(params: dict[str, Any], user_agent: str, retries: int = 3) -> dict[str, Any]:
    request = urllib.request.Request(
        API_ENDPOINT + "?" + urlencode(params),
        headers={
            "Accept": "application/json",
            "User-Agent": user_agent,
        },
    )
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            if exc.code < 500 and exc.code != 429:
                raise RuntimeError(f"MediaWiki API HTTP {exc.code}: {detail}") from exc
            last_error = RuntimeError(f"MediaWiki API HTTP {exc.code}: {detail}")
        except (urllib.error.URLError, TimeoutError, http.client.RemoteDisconnected) as exc:
            last_error = exc
        if attempt < retries:
            time.sleep(0.8 * (attempt + 1))
    raise RuntimeError(f"MediaWiki API request failed after retries: {last_error}")


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
            "formatversion": "2",
        },
        user_agent,
    )
    rows = data.get("query", {}).get("search", [])
    if not isinstance(rows, list):
        return []
    titles = []
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("title"), str):
            titles.append(row["title"])
    return titles


def fetch_pages(titles: list[str], user_agent: str, batch_size: int = 1) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    for start in range(0, len(titles), batch_size):
        batch = titles[start : start + batch_size]
        if not batch:
            continue
        data = request_json(
            {
                "action": "query",
                "format": "json",
                "prop": "extracts|info|pageprops",
                "explaintext": "1",
                "exsectionformat": "wiki",
                "inprop": "url",
                "titles": "|".join(batch),
                "formatversion": "2",
                "redirects": "1",
            },
            user_agent,
        )
        rows = data.get("query", {}).get("pages", [])
        if isinstance(rows, list):
            pages.extend(row for row in rows if isinstance(row, dict))
        time.sleep(0.15)
    return pages


def page_to_markdown(page: dict[str, Any], fetched_at: str, query: str) -> str:
    title = str(page.get("title") or "Untitled")
    extract = str(page.get("extract") or "").strip()
    url = str(page.get("fullurl") or "")
    lines = [
        f"# {title}",
        "",
        "<!-- source: minecraft_wiki_mediawiki_api -->",
        "",
        "## Metadata",
        "",
        f"- **Minecraft Wiki URL:** {url}",
        f"- **Page ID:** {page.get('pageid', '')}",
        f"- **Namespace:** {page.get('ns', '')}",
        f"- **Fetched at:** {fetched_at}",
    ]
    if query:
        lines.append(f"- **MCagent query:** {query}")
    lines.extend(["", "## Content", "", extract])
    return "\n".join(lines).strip() + "\n"


def seed_titles_for_query(query: str) -> list[str]:
    lowered = query.lower()
    titles: list[str] = []
    if any(token in lowered for token in ("玩法", "怎么玩", "survival", "creative", "gameplay", "mode")):
        titles.extend(["Gameplay", "Survival", "Creative", "Adventure", "Hardcore", "Spectator", "Multiplayer"])
    if any(token in lowered for token in ("合成", "craft", "配方")):
        titles.extend(["Crafting", "Recipe", "Crafting table"])
    if any(token in lowered for token in ("红石", "redstone")):
        titles.extend(["Redstone circuits", "Redstone Dust", "Redstone components"])
    if any(token in lowered for token in ("附魔", "enchant")):
        titles.extend(["Enchanting", "Enchantment", "Anvil"])
    if any(token in lowered for token in ("生物", "怪物", "mob")):
        titles.extend(["Mob", "Monster", "Animal", "Villager"])
    if any(token in lowered for token in ("维度", "下界", "末地", "nether", "end")):
        titles.extend(["Dimension", "The Nether", "The End"])
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


def fetch_seed(dest_root: Path, query: str, search_limit: int, core: bool, user_agent: str, force: bool = False) -> dict[str, Any]:
    run_dir = dest_root / "mediawiki" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    run_dir.mkdir(parents=True, exist_ok=True)
    fetched_at = datetime.now().isoformat(timespec="seconds")
    errors: list[dict[str, str]] = []
    ledger = load_ledger()

    titles = []
    titles.extend(seed_titles_for_query(query))
    titles.extend(search_titles(query, search_limit, user_agent))
    if core:
        titles.extend(CORE_TITLES)
    titles = unique_titles(titles)

    pages = []
    try:
        pages = fetch_pages(titles, user_agent)
    except RuntimeError as exc:
        errors.append({"stage": "fetch_pages", "error": str(exc)})

    records: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for page in pages:
        title = str(page.get("title") or "")
        extract = str(page.get("extract") or "").strip()
        if not title or not extract or page.get("missing") is not None:
            continue
        item_id = str(page.get("pageid") or title)
        url = str(page.get("fullurl") or "")
        key = make_key("mediawiki", item_id)
        digest = content_hash(extract)
        previous = ledger.get(key)
        if previous and previous.get("content_hash") == digest and not force:
            skipped.append(
                {
                    "title": title,
                    "pageid": page.get("pageid"),
                    "url": url,
                    "reason": "unchanged",
                    "previous_path": previous.get("path", ""),
                }
            )
            append_ledger(
                ledger_record(
                    source="mediawiki",
                    item_id=item_id,
                    title=title,
                    url=url,
                    text=extract,
                    path=str(previous.get("path", "")),
                    query=query,
                    status="skipped_unchanged",
                    previous=previous,
                )
            )
            continue
        filename = f"wiki_{slugify(title)}.md"
        path = run_dir / filename
        path.write_text(page_to_markdown(page, fetched_at, query), encoding="utf-8")
        status = "updated" if previous else "new"
        append_ledger(
            ledger_record(
                source="mediawiki",
                item_id=item_id,
                title=title,
                url=url,
                text=extract,
                path=str(path),
                query=query,
                status=status,
                previous=previous,
            )
        )
        records.append(
            {
                "title": title,
                "pageid": page.get("pageid"),
                "url": url,
                "path": str(path),
                "chars": len(extract),
                "status": status,
            }
        )

    manifest = {
        "manifest_type": "mediawiki_seed_export",
        "created_at": fetched_at,
        "api_endpoint": API_ENDPOINT,
        "user_agent": user_agent,
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
    parser = argparse.ArgumentParser(description="Fetch Minecraft Wiki MediaWiki API pages as Markdown for MCagent RAG.")
    parser.add_argument("--dest", default=str(PROJECT_ROOT / "data" / "crawler_exports"))
    parser.add_argument("--query", default="", help="MCagent question or search query.")
    parser.add_argument("--search-limit", type=int, default=12)
    parser.add_argument("--no-core", action="store_true", help="Do not include core gameplay seed pages.")
    parser.add_argument("--force", action="store_true", help="Write files even if the ledger says content is unchanged.")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    manifest = fetch_seed(
        dest_root=Path(args.dest).resolve(),
        query=args.query.strip(),
        search_limit=max(1, min(args.search_limit, 50)),
        core=not args.no_core,
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
