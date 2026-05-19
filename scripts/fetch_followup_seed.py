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
from urllib.parse import urljoin, urlparse
import urllib.error
import urllib.request


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from crawl_ledger import append_ledger, content_hash, ledger_record, load_ledger, make_key
from mcagent.cleaners import _HTMLTextExtractor, html_to_text, normalize_text


DEFAULT_USER_AGENT = "MC_Agent/0.1 (crawler followup; D:/magic/MC_Agent)"
PUBLIC_SOURCE_DIR = PROJECT_ROOT / "data" / "crawler_exports"
SKIP_HOST_PARTS = (
    "discord.",
    "curseforge.com",
    "maven.",
    "ko-fi.",
    "patreon.",
    "youtube.",
    "youtu.be",
)


def slugify(value: str, fallback: str = "page") -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-._")
    return cleaned[:90] or fallback


def request_text(url: str, user_agent: str, retries: int = 2) -> tuple[str, str]:
    request = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept": "text/html,text/plain,application/json,*/*"})
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                content_type = response.headers.get("Content-Type", "")
                raw = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
                return raw.decode(charset, errors="replace"), content_type
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
        if attempt < retries:
            time.sleep(0.8 * (attempt + 1))
    raise RuntimeError(f"Failed to fetch {url}: {last_error}")


def github_repo_parts(url: str) -> tuple[str, str] | None:
    parsed = urlparse(url)
    if parsed.netloc.lower() != "github.com":
        return None
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) < 2:
        return None
    if len(parts) >= 3 and parts[2].lower() in {"issues", "pulls", "releases", "actions"}:
        return None
    return parts[0], parts[1]


def github_raw_candidates(url: str) -> list[tuple[str, str, str]]:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    candidates: list[tuple[str, str, str]] = []
    if parsed.netloc.lower() == "raw.githubusercontent.com":
        title = "/".join(parts[-2:]) if len(parts) >= 2 else parsed.path.strip("/")
        candidates.append((title or "GitHub raw document", url, "raw"))
        return candidates
    repo = github_repo_parts(url)
    if not repo:
        return candidates
    owner, name = repo
    if len(parts) >= 3 and parts[2].lower() == "wiki":
        for page in ("Home", "_Sidebar"):
            candidates.append((f"{owner}/{name} wiki {page}", f"https://raw.githubusercontent.com/wiki/{owner}/{name}/{page}.md", "github_wiki"))
        return candidates
    for branch in ("main", "master"):
        for filename in ("README.md", "README.rst", "README.txt"):
            candidates.append((f"{owner}/{name} {branch} {filename}", f"https://raw.githubusercontent.com/{owner}/{name}/{branch}/{filename}", "github_readme"))
    return candidates


def unique_output_path(run_dir: Path, filename: str, digest: str) -> Path:
    path = run_dir / filename
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    return run_dir / f"{stem}_{digest[:8]}{suffix}"


def extract_urls_from_markdown(path: Path) -> list[dict[str, str]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    candidates: list[dict[str, str]] = []
    current_title = ""
    for line in text.splitlines():
        if line.startswith("# "):
            current_title = line.lstrip("#").strip()
        match = re.match(r"- \*\*(Source|Wiki|Issues|Discord|Modrinth URL):\*\* (.+)", line.strip(), flags=re.I)
        if not match:
            continue
        label, rest = match.group(1), match.group(2)
        urls = re.findall(r"https?://[^\s)>\]]+", rest)
        for url in urls:
            candidates.append({"label": label, "url": url.rstrip(".,;"), "title": current_title, "source_path": str(path)})
    return candidates


def query_tokens(query: str) -> list[str]:
    stopwords = {
        "the",
        "and",
        "for",
        "with",
        "mod",
        "mods",
        "boss",
        "bosses",
        "玩法",
        "怎么玩",
        "有哪些",
    }
    return [
        token
        for token in re.findall(r"[\w\u4e00-\u9fff]+", query.lower())
        if len(token) >= 3 and token not in stopwords
    ]


def candidate_matches_query(item: dict[str, str], tokens: list[str]) -> bool:
    if not tokens:
        return True
    haystack = " ".join([item.get("title", ""), item.get("source_path", ""), item.get("url", "")]).lower()
    return any(token in haystack for token in tokens)


def collect_candidates(source_dir: Path, include_issues: bool, query: str = "") -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    tokens = query_tokens(query)
    for path in sorted((source_dir / "modrinth_agent").rglob("*.md")) if (source_dir / "modrinth_agent").exists() else []:
        for item in extract_urls_from_markdown(path):
            label = item["label"].lower()
            if label == "modrinth url":
                continue
            if label == "issues" and not include_issues:
                continue
            if not candidate_matches_query(item, tokens):
                continue
            candidates.append(item)
    seen: set[str] = set()
    filtered: list[dict[str, str]] = []
    for item in candidates:
        url = item["url"]
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        if any(part in host for part in SKIP_HOST_PARTS):
            continue
        key = url.lower().rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        filtered.append(item)
    return filtered


def page_to_markdown(title: str, url: str, content: str, content_type: str, fetched_at: str, parent: dict[str, str]) -> str:
    tables = ""
    images = ""
    if "html" in content_type.lower() or re.search(r"<html|<body|<article|<main", content[:2000], flags=re.I):
        parser = _HTMLTextExtractor()
        parser.feed(content)
        parser.close()
        text = parser.text
        if parser.title:
            title = parser.title
        tables = tables_to_markdown(parser.tables)
        images = images_to_markdown(parser.images, url)
    elif content_type.lower().startswith("application/json"):
        try:
            value = json.loads(content)
            text = json.dumps(value, ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            text = content
    else:
        text = normalize_text(unescape(content))
    text = normalize_text(text)
    lines = [
        f"# {title or parent.get('title') or url}",
        "",
        "<!-- source: crawler_followup -->",
        "",
        "## Metadata",
        "",
        f"- **URL:** {url}",
        f"- **Parent title:** {parent.get('title', '')}",
        f"- **Parent source path:** {parent.get('source_path', '')}",
        f"- **Link label:** {parent.get('label', '')}",
        f"- **Fetched at:** {fetched_at}",
        "",
        "## Content",
        "",
        text,
    ]
    if tables:
        lines.extend(["", "## Extracted Tables", "", tables])
    if images:
        lines.extend(["", "## Extracted Images", "", images])
    return "\n".join(lines).strip() + "\n"


def tables_to_markdown(tables: list[list[list[str]]]) -> str:
    blocks: list[str] = []
    for index, table in enumerate(tables, start=1):
        rows = [[cell.strip() for cell in row] for row in table if any(cell.strip() for cell in row)]
        if not rows:
            continue
        width = max(len(row) for row in rows)
        normalized = [row + [""] * (width - len(row)) for row in rows]
        header = normalized[0]
        body = normalized[1:] or [[""] * width]
        block = [f"### Table {index}", "", "| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
        block.extend("| " + " | ".join(row) + " |" for row in body[:80])
        blocks.append("\n".join(block))
    return "\n\n".join(blocks)


def images_to_markdown(images: list[dict[str, str]], page_url: str) -> str:
    lines: list[str] = []
    seen: set[str] = set()
    for image in images:
        src = urljoin(page_url, image.get("src", ""))
        if not src or src in seen:
            continue
        seen.add(src)
        alt = image.get("alt") or "image"
        lines.append(f"- ![{alt}]({src})")
    return "\n".join(lines)


def fetch_followups(
    dest_root: Path,
    source_dir: Path,
    user_agent: str,
    max_urls: int,
    delay: float,
    include_issues: bool,
    force: bool,
    query: str = "",
) -> dict[str, Any]:
    run_dir = dest_root / "followup" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    run_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = run_dir / "raw_html"
    raw_dir.mkdir(parents=True, exist_ok=True)
    fetched_at = datetime.now().isoformat(timespec="seconds")
    ledger = load_ledger()
    records: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    candidates = collect_candidates(source_dir, include_issues=include_issues, query=query)
    expanded: list[dict[str, str]] = []
    seen_fetch_url: set[str] = set()
    for item in candidates:
        raw_candidates = github_raw_candidates(item["url"])
        if raw_candidates:
            for title, fetch_url, kind in raw_candidates:
                if fetch_url.lower() in seen_fetch_url:
                    continue
                seen_fetch_url.add(fetch_url.lower())
                expanded.append(item | {"fetch_url": fetch_url, "fetch_title": title, "kind": kind})
        else:
            fetch_url = item["url"]
            if fetch_url.lower() in seen_fetch_url:
                continue
            seen_fetch_url.add(fetch_url.lower())
            expanded.append(item | {"fetch_url": fetch_url, "fetch_title": item.get("title", "") or fetch_url, "kind": "generic"})

    for item in expanded[: max(1, max_urls)]:
        fetch_url = item["fetch_url"]
        title = item["fetch_title"]
        try:
            content, content_type = request_text(fetch_url, user_agent)
        except Exception as exc:  # noqa: BLE001
            # Missing README branches are expected; keep them as low-noise errors.
            if "HTTP Error 404" not in str(exc):
                errors.append({"url": fetch_url, "error": str(exc), "kind": item.get("kind", "")})
            continue
        if len(content.strip()) < 200:
            skipped.append({"url": fetch_url, "reason": "too_short", "kind": item.get("kind", "")})
            continue
        markdown = page_to_markdown(title, fetch_url, content, content_type, fetched_at, item)
        digest = content_hash(markdown)
        item_id = fetch_url.lower().rstrip("/")
        key = make_key("followup", item_id)
        previous = ledger.get(key)
        if previous and previous.get("content_hash") == digest and not force:
            skipped.append({"url": fetch_url, "reason": "unchanged", "previous_path": previous.get("path", ""), "kind": item.get("kind", "")})
            append_ledger(
                ledger_record(
                    source="followup",
                    item_id=item_id,
                    title=title,
                    url=fetch_url,
                    text=markdown,
                    path=str(previous.get("path", "")),
                    query=query or "followup",
                    status="skipped_unchanged",
                    previous=previous,
                )
            )
            continue
        filename = f"{slugify(item.get('kind', 'followup'))}_{slugify(title, 'page')}.md"
        path = unique_output_path(run_dir, filename, digest)
        raw_path = raw_dir / f"{path.stem}.html"
        path.write_text(markdown, encoding="utf-8")
        if "html" in content_type.lower() or re.search(r"<html|<body|<article|<main", content[:2000], flags=re.I):
            raw_path.write_text(content, encoding="utf-8")
        append_ledger(
            ledger_record(
                source="followup",
                item_id=item_id,
                title=title,
                url=fetch_url,
                text=markdown,
                path=str(path),
                query=query or "followup",
                status="updated" if previous else "new",
                previous=previous,
            )
        )
        records.append({
            "title": title,
            "url": fetch_url,
            "path": str(path),
            "raw_html_path": str(raw_path) if raw_path.exists() else "",
            "kind": item.get("kind", ""),
            "chars": len(markdown),
            "raw_html_chars": raw_path.stat().st_size if raw_path.exists() else 0,
        })
        time.sleep(delay)

    manifest = {
        "manifest_type": "crawler_followup_export",
        "created_at": fetched_at,
        "export_dir": str(run_dir),
        "source_dir": str(source_dir),
        "query": query,
        "candidates": len(candidates),
        "expanded_candidates": len(expanded),
        "records": records,
        "skipped": skipped,
        "errors": errors,
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Follow public source/wiki links from Modrinth exports and save Markdown for MCagent RAG.")
    parser.add_argument("--dest", default=str(PUBLIC_SOURCE_DIR))
    parser.add_argument("--source-dir", default=str(PUBLIC_SOURCE_DIR))
    parser.add_argument("--max-urls", type=int, default=30)
    parser.add_argument("--delay", type=float, default=0.2)
    parser.add_argument("--include-issues", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--query", default="", help="Only follow links from matching Modrinth export titles/paths/URLs.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    manifest = fetch_followups(
        dest_root=Path(args.dest).resolve(),
        source_dir=Path(args.source_dir).resolve(),
        user_agent=args.user_agent,
        max_urls=args.max_urls,
        delay=max(0.0, args.delay),
        include_issues=args.include_issues,
        force=args.force,
        query=args.query,
    )
    print(f"Exported to: {manifest['export_dir']}")
    print(f"Candidates: {manifest['candidates']} -> expanded {manifest['expanded_candidates']}")
    print(f"Markdown files: {len(manifest['records'])}")
    print(f"Skipped: {len(manifest['skipped'])}")
    print(f"Errors: {len(manifest['errors'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
