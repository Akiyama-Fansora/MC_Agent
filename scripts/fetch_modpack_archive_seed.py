from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import sys
import time
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlparse
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DEST = PROJECT_ROOT / "data" / "crawler_exports"
DEFAULT_ARCHIVE_ROOT = PROJECT_ROOT / "data" / "manual_research" / "modpack_archives"
DEFAULT_USER_AGENT = "MC_Agent/0.1 (modpack archive discovery; D:/magic/MC_Agent)"
MODRINTH_API = "https://api.modrinth.com/v2"
BBSMC_API = "https://api.bbsmc.net/v2"
ARCHIVE_EXTENSIONS = (".mrpack", ".zip")
CLOUD_DRIVE_DOMAINS = (
    "pan.quark.cn",
    "pan.baidu.com",
    "pan.xunlei.com",
    "www.123pan.com",
    "123pan.com",
    "www.123684.com",
    "123684.com",
)


def slugify(value: str, fallback: str = "modpack") -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._\-\u4e00-\u9fff]+", "-", value.strip()).strip("-._")
    return cleaned[:80] or fallback


def request_bytes(url: str, user_agent: str, timeout: int = 45) -> tuple[bytes, str, int]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "text/html,application/json,application/octet-stream,*/*",
            "User-Agent": user_agent,
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(), response.headers.get("Content-Type", ""), int(response.status)


def request_text(url: str, user_agent: str, timeout: int = 30) -> tuple[str, str, int]:
    raw, content_type, status = request_bytes(url, user_agent=user_agent, timeout=timeout)
    charset = "utf-8"
    match = re.search(r"charset=([^;\s]+)", content_type, flags=re.I)
    if match:
        charset = match.group(1)
    return raw.decode(charset, errors="replace"), content_type, status


def bing_rss(query: str, user_agent: str, limit: int) -> list[dict[str, Any]]:
    url = "https://www.bing.com/search?format=rss&mkt=zh-CN&setlang=zh-Hans&q=" + quote(query, safe="")
    try:
        text, _content_type, status = request_text(url, user_agent=user_agent, timeout=25)
        root = ET.fromstring(text)
    except Exception:
        return []
    results: list[dict[str, Any]] = []
    for rank, item in enumerate(root.findall("./channel/item"), start=1):
        link = "".join(item.findtext("link") or "").strip()
        if not link:
            continue
        results.append(
            {
                "engine": "bing_rss",
                "rank": rank,
                "status": status,
                "title": "".join(item.findtext("title") or "").strip(),
                "url": link,
                "snippet": "".join(item.findtext("description") or "").strip(),
            }
        )
        if len(results) >= limit:
            break
    return results


def modrinth_archive_candidates(query: str, user_agent: str, limit: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    candidates: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    search_url = (
        MODRINTH_API
        + "/search?limit="
        + str(max(1, min(limit, 10)))
        + "&facets="
        + quote('[["project_type:modpack"]]', safe="")
        + "&query="
        + quote(query, safe="")
    )
    try:
        text, _content_type, _status = request_text(search_url, user_agent=user_agent, timeout=30)
        data = json.loads(text)
    except Exception as exc:  # noqa: BLE001
        return [], [{"stage": "modrinth_search", "error": str(exc)}]
    for project in data.get("hits") or []:
        if not isinstance(project, dict):
            continue
        project_id = str(project.get("project_id") or project.get("slug") or "").strip()
        if not project_id:
            continue
        versions_url = f"{MODRINTH_API}/project/{quote(project_id, safe='')}/version"
        try:
            text, _content_type, _status = request_text(versions_url, user_agent=user_agent, timeout=35)
            versions = json.loads(text)
        except Exception as exc:  # noqa: BLE001
            errors.append({"stage": "modrinth_versions", "project": project_id, "error": str(exc)})
            continue
        for version in versions if isinstance(versions, list) else []:
            for file_info in version.get("files") or []:
                if not isinstance(file_info, dict):
                    continue
                filename = str(file_info.get("filename") or "")
                url = str(file_info.get("url") or "").strip()
                if not url or not filename.lower().endswith(".mrpack"):
                    continue
                candidates.append(
                    {
                        "source": "modrinth",
                        "project_title": project.get("title") or project.get("slug") or project_id,
                        "project_slug": project.get("slug"),
                        "project_url": "https://modrinth.com/modpack/" + str(project.get("slug") or project_id),
                        "version": version.get("version_number") or version.get("name") or "",
                        "filename": filename,
                        "url": url,
                        "size": file_info.get("size"),
                        "primary": file_info.get("primary"),
                    }
                )
                break
            if candidates:
                break
    return candidates, errors


def bbsmc_archive_candidates(query: str, user_agent: str, limit: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    candidates: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    projects = bbsmc_project_hits(query, user_agent=user_agent, limit=limit)
    seen_slugs: set[str] = set()
    for project in projects:
        slug = str(project.get("slug") or "").strip()
        project_id = str(project.get("project_id") or project.get("id") or "").strip()
        if not slug and not project_id:
            continue
        key = slug or project_id
        if key in seen_slugs:
            continue
        seen_slugs.add(key)
        project_url = f"https://bbsmc.net/modpack/{slug}" if slug else ""
        project_api_id = slug or project_id
        project_detail: dict[str, Any] = {}
        try:
            text, content_type, status = request_text(f"{BBSMC_API}/project/{quote(project_api_id, safe='')}", user_agent=user_agent, timeout=30)
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                project_detail = parsed
            pages.append(
                {
                    "engine": "bbsmc_api",
                    "rank": len(pages) + 1,
                    "status": status,
                    "content_type": content_type,
                    "title": str(project_detail.get("title") or project.get("title") or slug),
                    "url": project_url or f"{BBSMC_API}/project/{quote(project_api_id, safe='')}",
                    "api_url": f"{BBSMC_API}/project/{quote(project_api_id, safe='')}",
                    "slug": slug,
                    "project_id": project_id,
                    "project_type": project_detail.get("project_type") or project.get("project_type"),
                    "downloads": project_detail.get("downloads") or project.get("downloads"),
                    "game_versions": project_detail.get("game_versions") or project.get("versions"),
                    "loaders": project_detail.get("loaders"),
                    "description": project_detail.get("description") or project.get("description"),
                }
            )
        except Exception as exc:  # noqa: BLE001
            errors.append({"stage": "bbsmc_project", "project": project_api_id, "error": str(exc)})

        version_url = f"{BBSMC_API}/project/{quote(project_api_id, safe='')}/version"
        try:
            text, content_type, status = request_text(version_url, user_agent=user_agent, timeout=35)
            versions = json.loads(text)
        except Exception as exc:  # noqa: BLE001
            errors.append({"stage": "bbsmc_versions", "project": project_api_id, "error": str(exc)})
            continue
        if not isinstance(versions, list):
            errors.append({"stage": "bbsmc_versions", "project": project_api_id, "error": "version response was not a list"})
            continue
        pages.append(
            {
                "engine": "bbsmc_api",
                "rank": len(pages) + 1,
                "status": status,
                "content_type": content_type,
                "title": f"BBSMC versions for {project_detail.get('title') or project.get('title') or slug}",
                "url": version_url,
                "project_url": project_url,
                "slug": slug,
                "version_count": len(versions),
            }
        )
        for version in versions[: max(1, limit)]:
            if not isinstance(version, dict):
                continue
            version_brief = {
                "source": "bbsmc",
                "project_title": project_detail.get("title") or project.get("title") or slug,
                "project_slug": slug,
                "project_url": project_url,
                "project_id": project_id,
                "version": version.get("version_number") or version.get("name") or "",
                "version_name": version.get("name") or "",
                "date_published": version.get("date_published"),
                "downloads": version.get("downloads"),
                "game_versions": version.get("game_versions"),
                "loaders": version.get("loaders"),
                "disk_only": version.get("disk_only"),
            }
            files = version.get("files") if isinstance(version.get("files"), list) else []
            for file_info in files:
                if not isinstance(file_info, dict):
                    continue
                file_url = str(file_info.get("url") or "").strip()
                filename = str(file_info.get("filename") or Path(urlparse(file_url).path).name or "").strip()
                if not file_url:
                    continue
                item = version_brief | {
                    "filename": filename,
                    "url": file_url,
                    "size": file_info.get("size"),
                    "primary": file_info.get("primary"),
                    "file_type": file_info.get("file_type"),
                }
                if looks_like_direct_archive_url(file_url, filename):
                    candidates.append(item)
                elif looks_like_cloud_drive_url(file_url):
                    blockers.append(
                        item
                        | {
                            "stage": "bbsmc_file_url",
                            "blocker": "cloud_drive_or_client_only",
                            "reason": "BBSMC version file URL points to cloud storage, not an objective direct .mrpack/.zip archive URL.",
                        }
                    )
            disk_urls = version.get("disk_urls") if isinstance(version.get("disk_urls"), list) else []
            for disk in disk_urls:
                if not isinstance(disk, dict):
                    continue
                disk_url = str(disk.get("url") or "").strip()
                if not disk_url:
                    continue
                blockers.append(
                    version_brief
                    | {
                        "stage": "bbsmc_disk_url",
                        "blocker": "cloud_drive_or_client_only",
                        "platform": disk.get("platform"),
                        "url": disk_url,
                        "reason": "BBSMC exposes this version through a cloud-drive disk URL; full automation requires a direct public archive URL.",
                    }
                )
    return candidates, pages, blockers, errors


def bbsmc_project_hits(query: str, user_agent: str, limit: int) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    seen: set[str] = set()
    search_queries = [query]
    compact = re.sub(r"\s+", " ", query).strip()
    archive_terms_removed = re.sub(r"\b(?:modpack|minecraft|download|archive|mrpack|zip)\b|[.]", " ", compact, flags=re.I)
    archive_terms_removed = re.sub(r"\s+", " ", archive_terms_removed).strip()
    if archive_terms_removed and archive_terms_removed not in search_queries:
        search_queries.append(archive_terms_removed)
    slug_hint = bbsmc_slug_hint(query)
    if slug_hint:
        hits.append({"slug": slug_hint, "project_type": "modpack", "title": slug_hint})
        seen.add(slug_hint)
    for search_query in search_queries:
        search_url = f"{BBSMC_API}/search?query={quote(search_query, safe='')}"
        try:
            text, _content_type, _status = request_text(search_url, user_agent=user_agent, timeout=30)
            data = json.loads(text)
        except Exception:
            continue
        for hit in data.get("hits") or [] if isinstance(data, dict) else []:
            if not isinstance(hit, dict):
                continue
            if str(hit.get("project_type") or "").lower() not in {"", "modpack"}:
                continue
            key = str(hit.get("slug") or hit.get("project_id") or hit.get("id") or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            hits.append(hit)
            if len(hits) >= limit:
                return hits
    return hits


def bbsmc_slug_hint(value: str) -> str:
    match = re.search(r"bbsmc\.net/(?:modpack|project)/([^/?#]+)", value, flags=re.I)
    if match:
        return match.group(1).strip()
    match = re.search(r"api\.bbsmc\.net/v2/project/([^/?#]+)", value, flags=re.I)
    if match:
        return match.group(1).strip()
    return ""


def looks_like_direct_archive_url(url: str, filename: str = "") -> bool:
    path = urlparse(url).path.lower()
    lower_name = filename.lower()
    return any(path.endswith(ext) or lower_name.endswith(ext) for ext in ARCHIVE_EXTENSIONS)


def looks_like_cloud_drive_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(host == domain or host.endswith("." + domain) for domain in CLOUD_DRIVE_DOMAINS)


def archive_link_candidates(query: str, user_agent: str, limit: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    direct_candidate = direct_archive_candidate(query, user_agent=user_agent)
    if direct_candidate:
        return [direct_candidate], [], []
    search_queries = [
        query,
        f"{query} 整合包 下载",
        f"{query} modpack download",
        f"{query} mrpack",
        f"{query} zip",
    ]
    pages: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    seen_pages: set[str] = set()
    seen_links: set[str] = set()
    for search_query in search_queries:
        for page in bing_rss(search_query, user_agent=user_agent, limit=max(2, limit)):
            page_url = str(page.get("url") or "")
            if not page_url or page_url in seen_pages:
                continue
            seen_pages.add(page_url)
            pages.append(page | {"query": search_query})
            try:
                html, content_type, status = request_text(page_url, user_agent=user_agent, timeout=25)
            except Exception as exc:  # noqa: BLE001
                errors.append({"stage": "page_fetch", "url": page_url, "error": str(exc)})
                continue
            if any(page_url.lower().split("?", 1)[0].endswith(ext) for ext in ARCHIVE_EXTENSIONS):
                links = [page_url]
            else:
                hrefs = re.findall(r'''href\s*=\s*["']([^"']+)["']''', html, flags=re.I)
                links = [urljoin(page_url, unquote(href)) for href in hrefs]
            for link in links:
                normalized = link.split("#", 1)[0]
                parsed_path = urlparse(normalized).path.lower()
                if not any(parsed_path.endswith(ext) for ext in ARCHIVE_EXTENSIONS):
                    continue
                if normalized in seen_links:
                    continue
                seen_links.add(normalized)
                candidates.append(
                    {
                        "source": "web_page",
                        "page_title": page.get("title") or "",
                        "page_url": page_url,
                        "content_type": content_type,
                        "page_status": status,
                        "filename": Path(urlparse(normalized).path).name or "modpack.zip",
                        "url": normalized,
                        "query": search_query,
                    }
                )
                if len(candidates) >= limit:
                    return candidates, pages, errors
    return candidates, pages, errors


def direct_archive_candidate(value: str, user_agent: str) -> dict[str, Any] | None:
    target = value.strip()
    if not re.match(r"https?://", target, flags=re.I):
        return None
    parsed_path = urlparse(target).path.lower()
    if not any(parsed_path.endswith(ext) for ext in ARCHIVE_EXTENSIONS):
        return None
    candidate: dict[str, Any] = {
        "source": "direct_url",
        "filename": Path(urlparse(target).path).name or "modpack.zip",
        "url": target,
        "query": value,
    }
    try:
        request = urllib.request.Request(target, method="HEAD", headers={"User-Agent": user_agent, "Accept": "*/*"})
        with urllib.request.urlopen(request, timeout=30) as response:
            candidate.update(
                {
                    "http_status": int(response.status),
                    "content_type": response.headers.get("Content-Type", ""),
                    "content_length": response.headers.get("Content-Length", ""),
                    "final_url": response.geturl(),
                }
            )
    except Exception as exc:  # noqa: BLE001 - GET download may still work even if HEAD fails.
        candidate["head_error"] = str(exc)
    return candidate


def download_archive(candidate: dict[str, Any], archive_dir: Path, user_agent: str, max_bytes: int) -> dict[str, Any]:
    url = str(candidate.get("url") or "")
    filename = slugify(str(candidate.get("filename") or Path(urlparse(url).path).name or "modpack.zip"), "modpack.zip")
    if not any(filename.lower().endswith(ext) for ext in ARCHIVE_EXTENSIONS):
        suffix = Path(urlparse(url).path).suffix or ".zip"
        filename += suffix
    archive_dir.mkdir(parents=True, exist_ok=True)
    path = archive_dir / filename
    request = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept": "application/octet-stream,*/*"})
    with urllib.request.urlopen(request, timeout=120) as response:
        total = 0
        with path.open("wb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    handle.close()
                    try:
                        path.unlink()
                    except OSError:
                        pass
                    raise RuntimeError(f"archive exceeds max_bytes={max_bytes}")
                handle.write(chunk)
    return {"path": str(path), "bytes": path.stat().st_size}


def write_report(
    run_dir: Path,
    query: str,
    candidates: list[dict[str, Any]],
    downloads: list[dict[str, Any]],
    pages: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    blockers: list[dict[str, Any]] | None = None,
) -> Path:
    blockers = blockers or []
    lines = [
        f"# Modpack archive discovery for {query}",
        "",
        "<!-- source: modpack_download -->",
        "",
        "This document records public modpack archive discovery. It does not bypass login, payment, captcha, or private storage restrictions.",
        "",
        "## Downloaded Archives",
        "",
    ]
    if downloads:
        for item in downloads:
            lines.extend([f"- {item.get('path')}", f"  - source: {item.get('url')}", f"  - bytes: {item.get('bytes')}"])
    else:
        lines.append("- None")
    lines.extend(["", "## Archive Candidates", ""])
    if candidates:
        for item in candidates:
            lines.extend([f"- {item.get('filename') or item.get('project_title')}", f"  - url: {item.get('url')}", f"  - source: {item.get('source')}"])
    else:
        lines.append("- None")
    lines.extend(["", "## Access Blockers", ""])
    if blockers:
        for item in blockers[:50]:
            lines.extend(
                [
                    f"- {item.get('project_title') or item.get('platform') or item.get('url')}",
                    f"  - url: {item.get('url')}",
                    f"  - blocker: {item.get('blocker')}",
                    f"  - reason: {item.get('reason')}",
                    f"  - version: {item.get('version')}",
                ]
            )
    else:
        lines.append("- None")
    lines.extend(["", "## Searched Pages", ""])
    for page in pages[:30]:
        lines.append(f"- [{page.get('title') or page.get('url')}]({page.get('url')})")
    lines.extend(["", "## Errors", ""])
    if errors:
        for error in errors[:30]:
            lines.append(f"- {json.dumps(error, ensure_ascii=False)}")
    else:
        lines.append("- None")
    path = run_dir / "modpack_archive_discovery.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def discover_and_download(dest_root: Path, archive_root: Path, query: str, limit: int, download: bool, max_bytes: int, user_agent: str) -> dict[str, Any]:
    run_dir = dest_root / "modpack_download" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    run_dir.mkdir(parents=True, exist_ok=True)
    archive_dir = archive_root / slugify(query, "modpack") / "pack_archive"
    modrinth_candidates, modrinth_errors = modrinth_archive_candidates(query, user_agent=user_agent, limit=limit)
    bbsmc_candidates, bbsmc_pages, bbsmc_blockers, bbsmc_errors = bbsmc_archive_candidates(query, user_agent=user_agent, limit=limit)
    web_candidates, pages, web_errors = archive_link_candidates(query, user_agent=user_agent, limit=limit)
    candidates = modrinth_candidates + bbsmc_candidates + web_candidates
    blockers = bbsmc_blockers
    pages = bbsmc_pages + pages
    errors = modrinth_errors + bbsmc_errors + web_errors
    downloads: list[dict[str, Any]] = []
    if download:
        for candidate in candidates[:limit]:
            try:
                saved = download_archive(candidate, archive_dir, user_agent=user_agent, max_bytes=max_bytes)
            except Exception as exc:  # noqa: BLE001
                errors.append({"stage": "download", "url": candidate.get("url"), "error": str(exc)})
                continue
            downloads.append(candidate | saved)
            time.sleep(0.25)
            if downloads:
                break
    report = write_report(run_dir, query, candidates, downloads, pages, errors, blockers=blockers)
    records = [
        {
            "title": f"Modpack archive discovery for {query}",
            "url": "",
            "path": str(report),
            "chars": report.stat().st_size,
            "status": "new",
        }
    ]
    for item in downloads:
        records.append(
            {
                "title": f"Downloaded modpack archive: {Path(str(item.get('path'))).name}",
                "url": str(item.get("url") or ""),
                "path": str(item.get("path") or ""),
                "bytes": item.get("bytes"),
                "status": "new",
            }
        )
    manifest = {
        "manifest_type": "modpack_archive_discovery",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "provider": "modpack_download",
        "query": query,
        "export_dir": str(run_dir),
        "archive_dir": str(archive_dir),
        "records": records,
        "candidates": candidates,
        "downloads": downloads,
        "blockers": blockers,
        "search_results": pages,
        "skipped": [] if candidates else [{"reason": "no_public_archive_candidate_found", "blockers": len(blockers)}],
        "errors": errors,
        "failure_reason": ""
        if downloads
        else (
            "No public .mrpack/.zip archive was found or downloadable without login/payment/captcha restrictions. "
            + (f"Observed {len(blockers)} cloud-drive/client-only blocker(s)." if blockers else "")
        ).strip(),
        "next_action": "Run modpack_internal on the downloaded archive." if downloads else "CrawlerAgent should use browser/project pages or ask the user for an archive if no public package is available.",
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Discover and optionally download public Minecraft modpack archives for later internal parsing.")
    parser.add_argument("--dest", default=str(DEFAULT_DEST))
    parser.add_argument("--archive-root", default=str(DEFAULT_ARCHIVE_ROOT))
    parser.add_argument("--query", required=True)
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--no-download", action="store_true")
    parser.add_argument("--max-mb", type=int, default=700)
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    manifest = discover_and_download(
        dest_root=Path(args.dest).resolve(),
        archive_root=Path(args.archive_root).resolve(),
        query=args.query.strip(),
        limit=max(1, min(args.limit, 20)),
        download=not args.no_download,
        max_bytes=max(1, args.max_mb) * 1024 * 1024,
        user_agent=args.user_agent,
    )
    print(f"Exported to: {manifest['export_dir']}")
    print(f"Candidates: {len(manifest['candidates'])}")
    print(f"Downloads: {len(manifest['downloads'])}")
    print(f"Records: {len(manifest['records'])}")
    print(f"Errors: {len(manifest['errors'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
