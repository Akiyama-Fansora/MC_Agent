from __future__ import annotations

import argparse
import base64
from datetime import datetime
import hashlib
from html import unescape
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sys
import time
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DEST = PROJECT_ROOT / "data" / "crawler_exports"
DEFAULT_ARCHIVE_ROOT = PROJECT_ROOT / "data" / "manual_research" / "modpack_archives"
DEFAULT_USER_AGENT = "MC_Agent/0.1 (modpack archive discovery; D:/magic/MC_Agent)"
MODRINTH_API = "https://api.modrinth.com/v2"
BBSMC_API = "https://api.bbsmc.net/v2"
CFWIDGET_API = "https://api.cfwidget.com"
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
TEXT_EXTENSIONS = (".txt", ".md", ".json", ".html", ".ini")
URL_PATTERN = re.compile(r"https?://[A-Za-z0-9._~:/?#\[\]@!$&()*+,;=%-]+")
PUBLIC_SITE_METADATA_PATHS = (
    "/dw/get.txt",
    "/download/get.txt",
    "/downloads/get.txt",
    "/api/get.txt",
    "/get.txt",
    "/download.txt",
    "/version.txt",
    "/update.txt",
)
QUARK_API_HOST = "https://drive-pc.quark.cn"
SEARCH_SKIP_HOSTS = (
    "so.com",
    "360.cn",
    "360kan.com",
    "qhimg.com",
    "qhimgs4.com",
    "qhupdate.com",
    "360tres.com",
    "mediav.com",
    "bing.com",
    "w3.org",
    "requirejs.org",
    "hao.360.com",
    "crockford.com",
)
SOURCE_GRAPH_TERMS = ("MinePixel", "官网", "官方", "客户端", "服务器", "教程", "下载指南", "联机指南")


class LinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name.lower() in {"href", "src"} and value:
                self.links.append(value)


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


def decode_text_bytes(raw: bytes, content_type: str = "") -> str:
    encodings: list[str] = []
    match = re.search(r"charset=([^;\s]+)", content_type, flags=re.I)
    if match:
        encodings.append(match.group(1))
    encodings.extend(["utf-8-sig", "utf-8", "gb18030"])
    for encoding in encodings:
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def candidate_with_probe(candidate: dict[str, Any], user_agent: str, timeout: int = 45) -> dict[str, Any]:
    url = str(candidate.get("url") or "")
    headers = {"User-Agent": user_agent, "Accept": "application/octet-stream,*/*", "Range": "bytes=0-4095"}
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            sample = response.read(4096)
            candidate.update(
                {
                    "probe_status": int(response.status),
                    "probe_final_url": response.geturl(),
                    "probe_content_type": response.headers.get("Content-Type", ""),
                    "probe_content_length": response.headers.get("Content-Length", ""),
                    "probe_content_range": response.headers.get("Content-Range", ""),
                    "probe_magic": sample[:8].hex(),
                }
            )
            total = archive_total_bytes(response.headers.get("Content-Range", ""), response.headers.get("Content-Length", ""))
            if total:
                candidate["size"] = total
            if sample.startswith(b"PK\x03\x04"):
                candidate["archive_magic"] = "zip"
    except Exception as exc:  # noqa: BLE001
        candidate["probe_error"] = str(exc)
    return candidate


def archive_total_bytes(content_range: str, content_length: str) -> int | None:
    match = re.search(r"/(\d+)\s*$", content_range or "")
    if match:
        return int(match.group(1))
    if content_length and content_length.isdigit():
        return int(content_length)
    return None


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


def so_html_search(query: str, user_agent: str, limit: int) -> list[dict[str, Any]]:
    url = "https://www.so.com/s?q=" + quote(query, safe="")
    try:
        text, _content_type, status = request_text(url, user_agent=user_agent, timeout=30)
    except Exception:
        return []
    urls: list[str] = []
    for match in re.finditer(r'''(?:data-mdurl|data-url|href)\s*=\s*["']([^"']+)["']''', text, flags=re.I):
        urls.extend(search_result_url_variants(match.group(1)))
    for match in URL_PATTERN.finditer(text):
        urls.extend(search_result_url_variants(match.group(0)))
    results: list[dict[str, Any]] = []
    unique_urls = list(dict.fromkeys(urls))
    unique_urls.sort(key=lambda item: search_candidate_score(item, query), reverse=True)
    for candidate in unique_urls:
        host = urlparse(candidate).netloc.lower()
        if search_noise_host(host) or not usable_public_page_url(candidate):
            continue
        results.append(
            {
                "engine": "so_html",
                "status": status,
                "rank": len(results) + 1,
                "title": candidate,
                "url": candidate,
                "snippet": "URL candidate extracted from 360 search HTML.",
                "query": query,
            }
        )
        if len(results) >= limit:
            break
    return results


def search_candidate_score(url: str, query: str) -> int:
    lower_url = url.lower()
    score = 0
    if source_graph_host(url):
        score += 100
    for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}|[\u4e00-\u9fff]{2,}", query):
        token_lower = token.lower()
        if token_lower in lower_url:
            score += 25
    if any(ext in lower_url for ext in (".mrpack", ".zip", "download", "下载", "client", "客户端")):
        score += 10
    return score


def search_result_url_variants(raw: str) -> list[str]:
    candidates: list[str] = []
    value = unquote(unescape(raw)).rstrip(").,??")
    if not re.match(r"https?://[^/\s]+\.[^/\s]+", value):
        return []
    if value.startswith(("http://", "https://")):
        candidates.append(value)
    parsed = urlparse(value)
    if parsed.netloc.lower().endswith("so.com") and parsed.path.startswith("/link"):
        for values in parse_qs(parsed.query).values():
            for item in values:
                decoded = unquote(unescape(item)).strip()
                if decoded.startswith(("http://", "https://")):
                    candidates.append(decoded)
    return list(dict.fromkeys(item.split("#", 1)[0] for item in candidates if item))


def search_noise_host(host: str) -> bool:
    if not host:
        return True
    if re.match(r"^(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?$", host):
        return True
    if "$" in host:
        return True
    return any(host == item or host.endswith("." + item) for item in SEARCH_SKIP_HOSTS)

def query_variants(query: str) -> list[str]:
    variants = [query.strip()]
    lowered = query.lower()
    aliases = [
        (("乌托邦探险之旅", "乌托邦", "utopia", "utopian journey"), ["Utopian Journey", "Utopia Journey", "utopia-journey"]),
    ]
    for needles, additions in aliases:
        if any(needle in lowered or needle in query for needle in needles):
            variants.extend(["乌托邦 3.5.1Fix", "乌托邦之旅3.5Fix", *additions])
    compact = re.sub(r"\s+", " ", query.strip())
    if compact and compact != query.strip():
        variants.append(compact)
    plain = re.sub(r"\.(?:mrpack|zip)\b", " ", compact, flags=re.I)
    plain = re.sub(r"\b(?:minecraft|mc|modpack|download|archive|public|complete|data|fully|automatic|automatically|find|finding|collect|mrpack|zip)\b", " ", plain, flags=re.I)
    plain = re.sub(r"\s+", " ", plain).strip(" -_.,;:()[]{}")
    if plain:
        variants.append(plain)
    return list(dict.fromkeys(item for item in variants if item))


def archive_discovery_search_queries(query: str) -> list[str]:
    direct_queries: list[str] = []
    broad_queries: list[str] = []
    for variant in query_variants(query):
        direct_queries.extend(
            [
                variant,
                f"{variant} MinePixel",
                f"{variant} 官网",
                f"{variant} 官方",
                f"{variant} 客户端",
                f"{variant} 下载",
                f"{variant} 安装",
                f"{variant} 服务器",
                f"{variant} 联机指南",
            ]
        )
        broad_queries.extend(
            [
                f"{variant} 整合包 下载",
                f"{variant} 下载 指南",
                f"{variant} modpack download",
                f"{variant} server download",
                f"{variant} mrpack",
                f"{variant} zip",
            ]
        )
    return list(dict.fromkeys(item for item in direct_queries + broad_queries if item))


def official_site_search_queries(query: str) -> list[str]:
    queries: list[str] = []
    for variant in query_variants(query):
        queries.extend(
            [
                f"{variant} MinePixel",
                f"{variant} 官网",
                f"{variant} 官方网站",
                f"{variant} 服务器 官网",
                f"{variant} 客户端 官网",
                f"site:minebbs.com {variant}",
                f"{variant} MineBBS",
            ]
        )
    return list(dict.fromkeys(item for item in queries if item))


def inferred_official_site_urls(query: str) -> list[str]:
    lowered = query.lower()
    urls: list[str] = []
    if any(term in lowered or term in query for term in ("乌托邦探险之旅", "乌托邦之旅", "utopian journey", "utopia")):
        urls.extend(["https://www.minepixel.top/", "https://minepixel.top/"])
    return urls


def discover_official_site_pages(query: str, user_agent: str, limit: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    pages: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    seen: set[str] = set()
    for search_query in official_site_search_queries(query):
        hits = bing_rss(search_query, user_agent=user_agent, limit=max(3, limit))
        hits.extend(so_html_search(search_query, user_agent=user_agent, limit=max(3, limit)))
        hits.sort(key=lambda item: search_candidate_score(str(item.get("url") or ""), search_query), reverse=True)
        for page in hits:
            url = str(page.get("url") or "")
            if not url or url in seen or not usable_public_page_url(url):
                continue
            if not source_graph_host(url) and len(pages) >= limit:
                continue
            seen.add(url)
            pages.append(page | {"query": search_query, "discovery_method": "official_site_search"})
            if source_graph_host(url):
                return pages, errors
            if len(pages) >= limit:
                break
    return pages, errors

def discover_public_pages(query: str, user_agent: str, limit: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    pages: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    seen: set[str] = set()
    source_seen = False
    for search_query in archive_discovery_search_queries(query):
        query_hits = bing_rss(search_query, user_agent=user_agent, limit=max(2, min(limit, 5)))
        query_hits.extend(so_html_search(search_query, user_agent=user_agent, limit=max(2, min(limit, 5))))
        query_hits.sort(key=lambda item: search_candidate_score(str(item.get("url") or ""), search_query), reverse=True)
        accepted_this_query = 0
        for page in query_hits:
            page_url = str(page.get("url") or "")
            if not usable_public_page_url(page_url) or page_url in seen:
                continue
            if len(pages) >= limit and not source_graph_host(page_url):
                continue
            seen.add(page_url)
            pages.append(page | {"query": search_query})
            accepted_this_query += 1
            source_seen = source_seen or source_graph_host(page_url)
            if source_seen and len(pages) >= limit:
                return pages, errors
            if accepted_this_query >= 3 and not source_graph_host(page_url):
                break
    return pages, errors

def source_graph_host(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(
        marker in host
        for marker in (
            "minepixel",
            "modrinth",
            "curseforge",
            "github",
            "gitlab",
            "gitee",
            "cnb.cool",
            "minebbs",
        )
    )


def prioritize_source_pages(pages: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    ranked = list(pages)
    ranked.sort(key=lambda item: search_candidate_score(str(item.get("url") or ""), str(item.get("query") or item.get("title") or "")), reverse=True)
    return ranked[:limit]


def modrinth_archive_candidates(query: str, user_agent: str, limit: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    candidates: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    seen_projects: set[str] = set()
    for search_query in query_variants(query):
        search_url = (
            MODRINTH_API
            + "/search?limit="
            + str(max(1, min(limit, 10)))
            + "&facets="
            + quote('[["project_type:modpack"]]', safe="")
            + "&query="
            + quote(search_query, safe="")
        )
        try:
            text, _content_type, _status = request_text(search_url, user_agent=user_agent, timeout=30)
            data = json.loads(text)
        except Exception as exc:  # noqa: BLE001
            errors.append({"stage": "modrinth_search", "query": search_query, "error": str(exc)})
            continue
        for project in data.get("hits") or []:
            if not isinstance(project, dict):
                continue
            project_id = str(project.get("project_id") or project.get("slug") or "").strip()
            if not project_id or project_id in seen_projects:
                continue
            seen_projects.add(project_id)
            versions_url = f"{MODRINTH_API}/project/{quote(project_id, safe='')}/version"
            try:
                text, _content_type, _status = request_text(versions_url, user_agent=user_agent, timeout=35)
                versions = json.loads(text)
            except Exception as exc:  # noqa: BLE001
                errors.append({"stage": "modrinth_versions", "query": search_query, "project": project_id, "error": str(exc)})
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
                        candidate_with_probe(
                            {
                                "source": "modrinth",
                                "discovery_query": search_query,
                                "project_title": project.get("title") or project.get("slug") or project_id,
                                "project_slug": project.get("slug"),
                                "project_url": "https://modrinth.com/modpack/" + str(project.get("slug") or project_id),
                                "version": version.get("version_number") or version.get("name") or "",
                                "filename": filename,
                                "url": url,
                                "size": file_info.get("size"),
                                "primary": file_info.get("primary"),
                            },
                            user_agent=user_agent,
                            timeout=12,
                        )
                    )
                    break
                if candidates:
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
                    candidates.append(candidate_with_probe(item, user_agent=user_agent))
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


def slug_candidates_for_query(query: str) -> list[str]:
    slugs: list[str] = []
    for variant in query_variants(query):
        cleaned = re.sub(r"\.(?:mrpack|zip)\b", " ", variant, flags=re.I)
        cleaned = re.sub(r"\b(?:minecraft|mc|modpack|download|archive|public|complete|data|fully|automatic|automatically|find|finding|collect|mrpack|zip)\b", " ", cleaned, flags=re.I)
        cleaned = re.sub(r"[^A-Za-z0-9]+", "-", cleaned).strip("-").lower()
        if cleaned and len(cleaned) >= 3:
            slugs.append(cleaned)
    for match in re.finditer(r"curseforge\.com/minecraft/modpacks/([^/?#]+)", query, flags=re.I):
        slugs.insert(0, match.group(1).strip().lower())
    return list(dict.fromkeys(slugs))[:6]


def curseforge_mediafilez_url(file_id: int | str, filename: str) -> str:
    digits = re.sub(r"\D+", "", str(file_id or ""))
    safe_filename = quote(str(filename or "").strip(), safe="._-+()[] ")
    safe_filename = safe_filename.replace(" ", "%20")
    if len(digits) <= 3:
        return ""
    return f"https://mediafilez.forgecdn.net/files/{digits[:-3]}/{digits[-3:]}/{safe_filename}"


def curseforge_archive_candidates(query: str, user_agent: str, limit: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    candidates: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for slug in slug_candidates_for_query(query):
        api_url = f"{CFWIDGET_API}/minecraft/modpacks/{quote(slug, safe='')}"
        try:
            text, content_type, status = request_text(api_url, user_agent=user_agent, timeout=20)
            data = json.loads(text)
        except Exception as exc:  # noqa: BLE001
            errors.append({"stage": "curseforge_cfwidget", "slug": slug, "url": api_url, "error": str(exc)})
            continue
        title = str(data.get("title") or slug)
        pages.append(
            {
                "engine": "curseforge_cfwidget",
                "rank": len(pages) + 1,
                "status": status,
                "content_type": content_type,
                "title": title,
                "url": api_url,
                "project_id": data.get("id"),
                "summary": str(data.get("summary") or "")[:700],
            }
        )
        files: list[Any] = []
        download = data.get("download")
        if isinstance(download, dict):
            files.append(download)
        files.extend(item for item in data.get("files") or [] if isinstance(item, dict))
        for item in files:
            filename = str(item.get("name") or Path(urlparse(str(item.get("url") or "")).path).name or "").strip()
            file_id = item.get("id")
            if not filename.lower().endswith(".zip") or not file_id:
                continue
            direct_url = curseforge_mediafilez_url(file_id, filename)
            if not direct_url or direct_url in seen_urls:
                continue
            seen_urls.add(direct_url)
            candidate = candidate_with_probe(
                {
                    "source": "curseforge_cfwidget",
                    "discovery_query": slug,
                    "project_title": title,
                    "project_slug": slug,
                    "project_id": data.get("id"),
                    "project_url": f"https://www.curseforge.com/minecraft/modpacks/{slug}",
                    "file_page_url": item.get("url"),
                    "version": item.get("display") or item.get("version") or "",
                    "game_versions": item.get("versions") or [],
                    "filename": filename,
                    "url": direct_url,
                    "size": item.get("filesize"),
                    "downloads": item.get("downloads"),
                    "uploaded_at": item.get("uploaded_at"),
                    "discovery_method": "cfwidget project metadata -> CurseForge file id/name -> mediafilez.forgecdn.net direct archive URL",
                    "method_steps": [
                        f"Queried anonymous cfwidget endpoint {api_url}",
                        "Read project title/id and public file metadata",
                        "Derived mediafilez path from CurseForge file id grouping and filename",
                        "Range-probed direct URL for HTTP status, content type, size, and zip magic",
                    ],
                },
                user_agent=user_agent,
                timeout=15,
            )
            candidates.append(candidate)
            if len(candidates) >= limit:
                return candidates, pages, errors
        if candidates:
            break
    return candidates, pages, errors


def collect_public_site_pages(query: str, user_agent: str, limit: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    pages: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    seen: set[str] = set()
    site_urls = public_site_urls(query)
    site_urls.sort(key=site_probe_priority, reverse=True)
    for site_url in site_urls[: max(1, limit)]:
        if site_url in seen:
            continue
        seen.add(site_url)
        try:
            text, content_type, status = request_text(site_url, user_agent=user_agent, timeout=30)
        except Exception as exc:  # noqa: BLE001
            errors.append({"stage": "public_site_fetch", "url": site_url, "error": str(exc)})
            continue
        if parsed_is_metadata_url(site_url) and not extract_archive_urls(text, base_url=site_url):
            continue
        title = extract_title(text) or "Public modpack/community site"
        links = extract_links(text, site_url)
        pages.append(
            {
                "engine": "public_site",
                "rank": len(pages) + 1,
                "status": status,
                "content_type": content_type,
                "title": title,
                "url": site_url,
                "links": links[:80],
                "snippet": text[:700],
            }
        )
        for link in links:
            if not usable_public_page_url(link):
                continue
            for candidate in metadata_urls_for_site(link):
                if candidate not in site_urls:
                    site_urls.append(candidate)
    return pages, errors


def parsed_is_metadata_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith(TEXT_EXTENSIONS) or path in PUBLIC_SITE_METADATA_PATHS


def site_probe_priority(url: str) -> tuple[int, int, int]:
    path = urlparse(url).path.lower()
    direct_seed = 1 if path in {"/dw/get.txt", "/get.txt", "/download.txt", "/version.txt", "/update.txt"} else 0
    source = 1 if source_graph_host(url) else 0
    metadata = 1 if parsed_is_metadata_url(url) else 0
    return source, direct_seed, metadata


def mcmod_external_pages(query: str, user_agent: str, limit: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    pages: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    page_urls: list[str] = []
    for variant in query_variants(query):
        search_url = "https://search.mcmod.cn/s?key=" + quote(variant, safe="")
        try:
            search_html, _content_type, _status = request_text(search_url, user_agent=user_agent, timeout=30)
        except Exception as exc:  # noqa: BLE001
            errors.append({"stage": "mcmod_search", "url": search_url, "error": str(exc)})
        else:
            for link in extract_links(search_html, search_url):
                parsed = urlparse(link)
                if parsed.netloc.lower() in {"www.mcmod.cn", "mcmod.cn"} and "/modpack/" in parsed.path and link not in page_urls:
                    page_urls.append(link)
        for page in bing_rss(f"site:mcmod.cn {variant} 整合包", user_agent=user_agent, limit=max(2, limit)):
            url = str(page.get("url") or "")
            parsed = urlparse(url)
            if parsed.netloc.lower() in {"www.mcmod.cn", "mcmod.cn"} and "/modpack/" in parsed.path and url not in page_urls:
                page_urls.append(url)
    for match in URL_PATTERN.finditer(query):
        url = match.group(0).rstrip(").,，。")
        if "mcmod.cn/modpack/" in url and url not in page_urls:
            page_urls.append(url)
    seen: set[str] = set()
    for page_url in page_urls[: max(1, limit)]:
        try:
            text, content_type, status = request_text(page_url, user_agent=user_agent, timeout=30)
        except Exception as exc:  # noqa: BLE001
            errors.append({"stage": "mcmod_page_fetch", "url": page_url, "error": str(exc)})
            continue
        title = extract_title(text) or "MC百科 modpack page"
        links = mcmod_relevant_links(extract_links(text, page_url))
        pages.append(
            {
                "engine": "mcmod_external_links",
                "rank": len(pages) + 1,
                "status": status,
                "content_type": content_type,
                "title": title,
                "url": page_url,
                "links": links[:80],
                "snippet": text[:700],
            }
        )
        for link in links:
            if link in seen:
                continue
            seen.add(link)
            pages.append(
                {
                    "engine": "mcmod_decoded_external_link",
                    "rank": len(pages) + 1,
                    "status": status,
                    "content_type": "",
                    "title": "Decoded MC百科 external link",
                    "url": link,
                    "links": [],
                    "snippet": f"Decoded from {page_url}",
                }
            )
            if len(pages) >= limit * 3:
                return pages, errors
    return pages, errors


def mcmod_relevant_links(links: list[str]) -> list[str]:
    relevant: list[str] = []
    for link in links:
        parsed = urlparse(link)
        host = parsed.netloc.lower()
        if not host:
            continue
        if host in {"www.mcmod.cn", "mcmod.cn"} or host.endswith(".mcmod.cn"):
            continue
        if looks_like_cloud_drive_url(link) or usable_public_page_url(link):
            relevant.append(link)
    return list(dict.fromkeys(relevant))


def public_site_urls(query: str) -> list[str]:
    urls: list[str] = []
    for match in URL_PATTERN.finditer(query):
        url = match.group(0)
        clean = url.rstrip(").,，。")
        parsed = urlparse(clean)
        if parsed.netloc and not looks_like_cloud_drive_url(clean):
            urls.append(clean)
            urls.extend(metadata_urls_for_site(clean))
    return list(dict.fromkeys(urls))


def usable_public_page_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    if parsed.path.lower().endswith((".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".woff", ".woff2")):
        return False
    if looks_like_cloud_drive_url(url):
        return False
    host = parsed.netloc.lower()
    noisy_hosts = (
        "youtube.com",
        "youtu.be",
        "bilibili.com",
        "zhihu.com",
        "baidu.com",
        "google.com",
        "google.co.th",
        "google.com.hk",
        "google.co.kr",
        "googleapis.com",
        "gweb-interland.appspot.com",
        "collinsdictionary.com",
        "merriam-webster.com",
        "googlesyndication.com",
        "360.cn",
        "360kan.com",
        "qhimg.com",
        "qhimgs4.com",
        "qhupdate.com",
        "mediav.com",
    )
    return not any(host == item or host.endswith("." + item) for item in noisy_hosts)


def metadata_urls_for_site(url: str) -> list[str]:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return []
    base = f"{parsed.scheme}://{parsed.netloc}"
    paths: list[str] = []
    if parsed.path.lower().endswith(TEXT_EXTENSIONS):
        paths.append(parsed.path)
    paths.extend(PUBLIC_SITE_METADATA_PATHS)
    return list(dict.fromkeys(urljoin(base, path) for path in paths))


def extract_links(text: str, base_url: str) -> list[str]:
    parser = LinkExtractor()
    try:
        parser.feed(text)
    except Exception:
        pass
    urls = [urljoin(base_url, unquote(link)).split("#", 1)[0] for link in parser.links]
    urls.extend(match.group(0).rstrip(").,，。") for match in URL_PATTERN.finditer(text))
    expanded: list[str] = []
    for url in urls:
        expanded.append(url)
        expanded.extend(decode_known_redirect_url(url))
    return list(dict.fromkeys(expanded))


def decode_known_redirect_url(url: str) -> list[str]:
    parsed = urlparse(url)
    if parsed.netloc.lower() not in {"link.mcmod.cn", "www.link.mcmod.cn"}:
        return []
    match = re.search(r"/target/([^/?#]+)", parsed.path)
    if not match:
        return []
    token = match.group(1)
    padding = "=" * ((4 - len(token) % 4) % 4)
    try:
        decoded = base64.urlsafe_b64decode((token + padding).encode("ascii")).decode("utf-8")
    except Exception:
        return []
    return [decoded] if decoded.startswith(("http://", "https://")) else []


def extract_title(text: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", text, flags=re.I | re.S)
    if not match:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", match.group(1))).strip()[:180]


def yuque_doc_pages(query: str, user_agent: str, limit: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    pages: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for doc_url in yuque_doc_urls(query)[: max(1, limit)]:
        parsed = urlparse(doc_url)
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 3:
            continue
        user_login, book_slug, doc_slug = parts[0], parts[1], parts[2]
        api_url = (
            f"https://www.yuque.com/api/docs/{quote(doc_slug, safe='')}"
            f"?user_login={quote(user_login, safe='')}&book_slug={quote(book_slug, safe='')}&id={quote(doc_slug, safe='')}"
            "&include_contributors=true&include_like=true&include_hits=true&merge_dynamic_data=true"
        )
        try:
            text, content_type, status = request_text(api_url, user_agent=user_agent, timeout=30)
            data = json.loads(text)
            doc = data.get("data") if isinstance(data, dict) else {}
            if not isinstance(doc, dict):
                raise ValueError("Yuque API data was not an object")
            content = "\n".join(str(doc.get(key) or "") for key in ("title", "description", "content"))
        except Exception as exc:  # noqa: BLE001
            errors.append({"stage": "yuque_doc_fetch", "url": doc_url, "api_url": api_url, "error": str(exc)})
            continue
        pages.append(
            {
                "engine": "yuque_api",
                "rank": len(pages) + 1,
                "status": status,
                "content_type": content_type,
                "title": str(doc.get("title") or "Yuque document"),
                "url": doc_url,
                "api_url": api_url,
                "links": extract_links(content, doc_url)[:80],
                "snippet": re.sub(r"<[^>]+>", " ", content)[:900],
            }
        )
    return pages, errors


def yuque_doc_urls(query: str) -> list[str]:
    urls: list[str] = []
    for match in URL_PATTERN.finditer(query):
        url = match.group(0)
        clean = url.rstrip(").,，。")
        if urlparse(clean).netloc.lower().endswith("yuque.com"):
            urls.append(clean)
    return list(dict.fromkeys(urls))


def cloud_drive_observations(pages: list[dict[str, Any]], query: str, user_agent: str, limit: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    blockers: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    share_urls: list[str] = []
    for page in pages:
        for link in page.get("links") or []:
            if isinstance(link, str) and looks_like_cloud_drive_url(link):
                share_urls.append(link)
    for match in URL_PATTERN.finditer(query):
        url = match.group(0)
        if looks_like_cloud_drive_url(url):
            share_urls.append(url.rstrip(").,，。"))
    for share_url in list(dict.fromkeys(share_urls))[: max(1, limit)]:
        if "pan.quark.cn" in urlparse(share_url).netloc.lower():
            quark_blockers, quark_errors = inspect_quark_share(share_url, query=query, user_agent=user_agent)
            blockers.extend(quark_blockers)
            errors.extend(quark_errors)
        else:
            blockers.append(
                {
                    "stage": "cloud_drive_link",
                    "blocker": "cloud_drive_or_client_only",
                    "url": share_url,
                    "reason": "Cloud-drive URL was discovered; full automation still requires an objective no-login direct archive URL.",
                }
            )
    return blockers, errors


def inspect_quark_share(share_url: str, query: str, user_agent: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    blockers: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    match = re.search(r"/s/([A-Za-z0-9_-]+)", share_url)
    if not match:
        return blockers, [{"stage": "quark_parse", "url": share_url, "error": "could not parse share id"}]
    pwd_id = match.group(1)
    headers = {
        "User-Agent": user_agent,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": "https://pan.quark.cn",
        "Referer": share_url,
    }
    try:
        token_raw = post_json(
            QUARK_API_HOST + "/1/clouddrive/share/sharepage/token",
            {"pwd_id": pwd_id, "passcode": ""},
            headers=headers,
            timeout=30,
        )
        stoken = str(token_raw.get("data", {}).get("stoken") or "")
    except Exception as exc:  # noqa: BLE001
        return blockers, [{"stage": "quark_token", "url": share_url, "error": str(exc)}]
    if not stoken:
        return blockers, [{"stage": "quark_token", "url": share_url, "error": "missing stoken"}]
    queue = ["0"]
    seen_dirs: set[str] = set()
    file_count = 0
    while queue and len(seen_dirs) < 16 and file_count < 80:
        dir_fid = queue.pop(0)
        if dir_fid in seen_dirs:
            continue
        seen_dirs.add(dir_fid)
        try:
            detail = quark_list_dir(pwd_id, stoken, dir_fid, headers=headers)
        except Exception as exc:  # noqa: BLE001
            errors.append({"stage": "quark_list", "url": share_url, "dir_fid": dir_fid, "error": str(exc)})
            continue
        for item in detail.get("data", {}).get("list", []) if isinstance(detail.get("data"), dict) else []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("file_name") or "")
            fid = str(item.get("fid") or "")
            if item.get("dir"):
                if relevant_quark_name(name, query):
                    queue.append(fid)
                continue
            file_count += 1
            size = int(item.get("size") or 0)
            token = str(item.get("share_fid_token") or "")
            entry = {
                "stage": "quark_share_listing",
                "blocker": "cloud_drive_login_required_for_download",
                "platform": "quark",
                "url": share_url,
                "filename": name,
                "size": size,
                "fid": fid,
                "share_fid_token_present": bool(token),
                "reason": "Quark share can be listed anonymously, but the share download endpoint requires login; this is evidence to change route, not a completed archive download.",
            }
            if relevant_quark_name(name, query):
                try:
                    download_attempt = quark_try_download(pwd_id, stoken, fid, token, headers=headers)
                    entry["download_probe"] = download_attempt
                except Exception as exc:  # noqa: BLE001
                    entry["download_probe"] = {"error": str(exc)}
                blockers.append(entry)
    return blockers, errors


def relevant_quark_name(name: str, query: str) -> bool:
    combined = f"{name}\n{query}".lower()
    if looks_like_direct_archive_url("https://example.invalid/" + quote(name), name):
        return True
    if name.lower().endswith(".exe") and int(bool(re.search(r"安装|installer|setup|一键", name, flags=re.I))):
        return True
    return any(term in combined for term in ("乌托邦", "utopia", "utopian", "minepixel", "整合包", "modpack"))


def post_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: int) -> dict[str, Any]:
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = {"body": body}
        parsed["http_status"] = exc.code
        return parsed


def quark_list_dir(pwd_id: str, stoken: str, pdir_fid: str, headers: dict[str, str]) -> dict[str, Any]:
    params = {
        "pwd_id": pwd_id,
        "stoken": stoken,
        "pdir_fid": pdir_fid,
        "force": "0",
        "_page": "1",
        "_size": "100",
        "_fetch_banner": "1" if pdir_fid == "0" else "0",
        "_fetch_share": "1" if pdir_fid == "0" else "0",
        "_fetch_total": "1",
        "fetch_sub_file_cnt": "1",
        "ver": "2",
        "format": "png",
        "support_visit_limit_private_share": "true",
        "fetch_share_full_path": "0",
    }
    url = QUARK_API_HOST + "/1/clouddrive/share/sharepage/detail?" + "&".join(
        f"{quote(str(key), safe='')}={quote(str(value), safe='')}" for key, value in params.items()
    )
    request = urllib.request.Request(url, headers={key: value for key, value in headers.items() if key.lower() != "content-type"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def quark_try_download(pwd_id: str, stoken: str, fid: str, fid_token: str, headers: dict[str, str]) -> dict[str, Any]:
    data = post_json(
        QUARK_API_HOST + "/1/clouddrive/file/share/download",
        {"fids": [fid], "pwd_id": pwd_id, "stoken": stoken, "fids_token": [fid_token]},
        headers=headers,
        timeout=30,
    )
    return {
        "http_status": data.get("http_status") or data.get("status"),
        "code": data.get("code"),
        "message": data.get("message"),
        "has_download_url": bool(data.get("data")),
    }


def public_release_candidates(
    query: str, user_agent: str, limit: int, discovery_pages: list[dict[str, Any]] | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    pages: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    seen: set[str] = set()
    seed_urls = release_seed_urls(query, pages=discovery_pages)
    prioritized = [url for url in seed_urls if source_graph_host(url)]
    seed_urls = list(dict.fromkeys(prioritized + seed_urls))
    for seed_url in seed_urls:
        if seed_url in seen:
            continue
        seen.add(seed_url)
        try:
            text, content_type, status = request_text(seed_url, user_agent=user_agent, timeout=30)
        except Exception as exc:  # noqa: BLE001
            errors.append({"stage": "release_seed_fetch", "url": seed_url, "error": str(exc)})
            continue
        pages.append(
            {
                "engine": "public_release_seed",
                "rank": len(pages) + 1,
                "status": status,
                "content_type": content_type,
                "title": "Public release/download seed",
                "url": seed_url,
                "snippet": text[:400],
            }
        )
        for archive_url in extract_archive_urls(text, base_url=seed_url):
            if archive_url in seen:
                continue
            seen.add(archive_url)
            candidates.append(
                candidate_with_probe(
                    {
                        "source": "public_release_seed",
                        "page_url": seed_url,
                        "filename": Path(urlparse(archive_url).path).name or "modpack.zip",
                        "url": archive_url,
                        "query": query,
                        "discovery_method": "source graph page -> public site metadata endpoint -> direct archive URL",
                        "method_steps": [
                            "search or source page exposed a public/community site",
                            "metadata_urls_for_site probed small public text endpoints such as /dw/get.txt",
                            "seed text exposed a direct .zip/.mrpack URL",
                            "Range probe recorded HTTP status, content-type, size, redirect, and archive magic for CrawlerAgent to judge",
                        ],
                    },
                    user_agent=user_agent,
                )
            )
            if len(candidates) >= limit:
                return candidates, pages, errors
    return candidates, pages, errors


def xye_release_pages(pages: list[dict[str, Any]], user_agent: str, limit: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    output: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    resource_ids: list[str] = []
    for page in pages:
        urls = [str(page.get("url") or "")]
        urls.extend(str(link) for link in page.get("links") or [] if isinstance(link, str))
        for url in urls:
            if "xyebbs.com" not in url:
                continue
            try:
                text, content_type, status = request_text(url, user_agent=user_agent, timeout=30)
            except Exception as exc:  # noqa: BLE001
                errors.append({"stage": "xye_page_fetch", "url": url, "error": str(exc)})
                continue
            resource_ids.extend(re.findall(r'\\"resourceId\\":(\d+)', text))
            output.append(
                {
                    "engine": "xyebbs_page",
                    "rank": len(output) + 1,
                    "status": status,
                    "content_type": content_type,
                    "title": extract_title(text) or "XyeBBS resource page",
                    "url": url,
                    "links": extract_links(text, url)[:80],
                    "snippet": text[:900],
                }
            )
            if len(output) >= limit:
                break
        if len(output) >= limit:
            break
    for resource_id in list(dict.fromkeys(resource_ids))[: max(1, limit)]:
        api_url = f"https://resource-api.xyeidc.com/client/resources/{resource_id}/releases?page=1&per=100&includes=versions,cores,links"
        try:
            text, content_type, status = request_text(api_url, user_agent=user_agent, timeout=30)
            data = json.loads(text)
        except Exception as exc:  # noqa: BLE001
            errors.append({"stage": "xye_releases_fetch", "url": api_url, "error": str(exc)})
            continue
        releases = (data.get("data") or {}).get("data") if isinstance(data, dict) else []
        summary_lines = []
        for release in releases or []:
            if not isinstance(release, dict):
                continue
            summary_lines.append(
                f"{release.get('label')}: file={release.get('fileName') or release.get('fileUuid') or 'none'} "
                f"links={len(release.get('links') or [])} downloads={release.get('downloadCount')} "
                f"updated={release.get('updateDate')}"
            )
        output.append(
            {
                "engine": "xyebbs_public_api",
                "rank": len(output) + 1,
                "status": status,
                "content_type": content_type,
                "title": f"XyeBBS public releases for resource {resource_id}",
                "url": api_url,
                "links": [],
                "snippet": "\n".join(summary_lines)[:1200],
                "release_count": len(releases or []),
            }
        )
    return output, errors


def release_seed_urls(query: str, pages: list[dict[str, Any]] | None = None) -> list[str]:
    urls: list[str] = []
    for match in URL_PATTERN.finditer(query):
        url = match.group(0)
        clean = url.rstrip(").,，。")
        parsed = urlparse(clean)
        if parsed.path.lower().endswith(TEXT_EXTENSIONS):
            urls.append(clean)
    for site_url in public_site_urls(query):
        urls.extend(metadata_urls_for_site(site_url))
    for site_url in inferred_official_site_urls(query):
        urls.extend(metadata_urls_for_site(site_url))
    for page in pages or []:
        page_url = str(page.get("url") or "")
        if usable_public_page_url(page_url) and (source_graph_host(page_url) or len(urls) < 40):
            urls.extend(metadata_urls_for_site(page_url))
        for link in page.get("links") or []:
            if isinstance(link, str) and usable_public_page_url(link) and (source_graph_host(link) or len(urls) < 40):
                urls.extend(metadata_urls_for_site(link))
    return urls


def extract_archive_urls(text: str, base_url: str) -> list[str]:
    urls: list[str] = []
    for match in URL_PATTERN.finditer(text):
        clean = match.group(0).rstrip(").,，。")
        if looks_like_direct_archive_url(clean, Path(urlparse(clean).path).name):
            urls.append(clean)
    for match in re.finditer(r'''href\s*=\s*["']([^"']+)["']''', text, flags=re.I):
        clean = urljoin(base_url, unquote(match.group(1))).split("#", 1)[0]
        if looks_like_direct_archive_url(clean, Path(urlparse(clean).path).name):
            urls.append(clean)
    return list(dict.fromkeys(urls))


def archive_link_candidates(query: str, user_agent: str, limit: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    direct_candidate = direct_archive_candidate(query, user_agent=user_agent)
    if direct_candidate:
        return [direct_candidate], [], []
    search_queries = archive_discovery_search_queries(query)
    pages: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    seen_pages: set[str] = set()
    seen_links: set[str] = set()
    for search_query in search_queries:
        search_pages = bing_rss(search_query, user_agent=user_agent, limit=max(2, limit)) + so_html_search(search_query, user_agent=user_agent, limit=max(2, limit))
        for page in search_pages:
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
    return candidate_with_probe(candidate, user_agent=user_agent)


def download_archive(candidate: dict[str, Any], archive_dir: Path, user_agent: str, max_bytes: int, timeout: int = 1800) -> dict[str, Any]:
    url = str(candidate.get("url") or "")
    filename = slugify(str(candidate.get("filename") or Path(urlparse(url).path).name or "modpack.zip"), "modpack.zip")
    if not any(filename.lower().endswith(ext) for ext in ARCHIVE_EXTENSIONS):
        suffix = Path(urlparse(url).path).suffix or ".zip"
        filename += suffix
    archive_dir.mkdir(parents=True, exist_ok=True)
    path = archive_dir / filename
    if path.exists():
        validation = validate_archive(path)
        if validation.get("is_zip"):
            return {
                "path": str(path),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
                "validation": validation,
                "reused_existing": True,
            }
    part_path = path.with_name(path.name + ".part")
    if not archive_candidate_is_downloadable(candidate):
        raise RuntimeError("candidate was not objectively verified as a downloadable zip/mrpack archive")
    remote_size = archive_candidate_size(candidate)
    if remote_size and remote_size > max_bytes:
        raise RuntimeError(f"archive exceeds max_bytes={max_bytes}")
    if remote_size:
        ranged_download(url, part_path, user_agent=user_agent, total_size=remote_size, timeout=timeout)
    else:
        plain_download(url, part_path, user_agent=user_agent, max_bytes=max_bytes, timeout=timeout)
    validation = validate_archive(part_path)
    if not validation.get("is_zip"):
        try:
            part_path.unlink()
        except OSError:
            pass
        raise RuntimeError("downloaded file is not a readable zip archive")
    try:
        part_path.replace(path)
    except OSError:
        if path.exists():
            path.unlink()
        part_path.replace(path)
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": sha256_file(path), "validation": validation}


def archive_candidate_size(candidate: dict[str, Any]) -> int | None:
    value = candidate.get("size") or candidate.get("content_length") or candidate.get("probe_content_length")
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return archive_total_bytes(str(candidate.get("probe_content_range") or ""), str(candidate.get("probe_content_length") or ""))


def archive_candidate_is_downloadable(candidate: dict[str, Any]) -> bool:
    if str(candidate.get("archive_magic") or "").lower() == "zip":
        return True
    magic = str(candidate.get("probe_magic") or "").lower()
    if magic.startswith("504b0304"):
        return True
    return False


def target_name_terms(query: str) -> list[str]:
    value = re.sub(r"\.(?:mrpack|zip)\b", " ", str(query or ""), flags=re.I)
    words = re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}|[\u4e00-\u9fff]{2,}", value)
    generic = {
        "minecraft",
        "modpack",
        "download",
        "archive",
        "public",
        "complete",
        "data",
        "fully",
        "automatic",
        "automatically",
        "mrpack",
        "zip",
    }
    return [word for word in words if word.lower() not in generic][:6]


def archive_candidate_matches_target(candidate: dict[str, Any], query: str) -> bool:
    terms = target_name_terms(query)
    if not terms:
        return True
    combined = " ".join(
        str(candidate.get(key) or "")
        for key in ("project_title", "project_slug", "filename", "project_url", "url")
    ).lower()
    return any(term.lower() in combined for term in terms)


def archive_candidate_rank(candidate: dict[str, Any], query: str) -> tuple[int, int, int, int, int]:
    url = str(candidate.get("url") or "")
    page_url = str(candidate.get("page_url") or candidate.get("project_url") or "")
    filename = str(candidate.get("filename") or "")
    combined = f"{url} {page_url} {filename} {candidate.get('project_title') or ''}".lower()
    verified = 1 if archive_candidate_is_downloadable(candidate) else 0
    sized = 1 if archive_candidate_size(candidate) else 0
    source_graph = 1 if source_graph_host(url) or source_graph_host(page_url) else 0
    target_match = 1 if archive_candidate_matches_target(candidate, query) else 0
    public_seed = 1 if str(candidate.get("source") or "") == "public_release_seed" else 0
    return verified, sized, source_graph + public_seed, target_match, search_candidate_score(url + " " + page_url, query)


def prioritize_archive_candidates(candidates: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    ranked = list(candidates)
    ranked.sort(key=lambda item: archive_candidate_rank(item, query), reverse=True)
    return ranked


def ranged_download(url: str, part_path: Path, *, user_agent: str, total_size: int, timeout: int) -> None:
    part_path.parent.mkdir(parents=True, exist_ok=True)
    offset = part_path.stat().st_size if part_path.exists() else 0
    if offset > total_size:
        part_path.unlink()
        offset = 0
    window = 32 * 1024 * 1024
    with part_path.open("ab") as handle:
        while offset < total_size:
            end = min(total_size - 1, offset + window - 1)
            headers = {
                "User-Agent": user_agent,
                "Accept": "application/octet-stream,*/*",
                "Range": f"bytes={offset}-{end}",
            }
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if int(response.status) not in {200, 206}:
                    raise RuntimeError(f"unexpected archive download status={response.status}")
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    offset += len(chunk)
                handle.flush()
            if offset <= end:
                raise RuntimeError(f"range download stalled at byte {offset}")
    if part_path.stat().st_size != total_size:
        raise RuntimeError(f"range download size mismatch: got {part_path.stat().st_size}, expected {total_size}")


def plain_download(url: str, part_path: Path, *, user_agent: str, max_bytes: int, timeout: int) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept": "application/octet-stream,*/*"})
    try:
        part_path.unlink()
    except OSError:
        pass
    with urllib.request.urlopen(request, timeout=timeout) as response:
        total = 0
        with part_path.open("wb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    handle.close()
                    try:
                        part_path.unlink()
                    except OSError:
                        pass
                    raise RuntimeError(f"archive exceeds max_bytes={max_bytes}")
                handle.write(chunk)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_archive(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"is_zip": False}
    try:
        with zipfile.ZipFile(path) as zipped:
            names = zipped.namelist()
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)
        return result
    minecraft_versions = [name for name in names if name.startswith(".minecraft/versions/") and name.endswith("/") and name.count("/") == 3]
    result.update(
        {
            "is_zip": True,
            "entries": len(names),
            "has_manifest_json": "manifest.json" in names,
            "has_modlist_html": "modlist.html" in names,
            "minecraft_instance_dirs": minecraft_versions[:10],
            "has_minecraft_version_instance": bool(minecraft_versions),
            "sample_entries": names[:40],
        }
    )
    if minecraft_versions:
        root = minecraft_versions[0]
        result.update(
            {
                "instance_root": root,
                "mods_count": sum(1 for name in names if name.startswith(root + "mods/") and name.lower().endswith(".jar")),
                "config_count": sum(1 for name in names if name.startswith(root + "config/") and not name.endswith("/")),
                "kubejs_count": sum(1 for name in names if name.startswith(root + "kubejs/") and not name.endswith("/")),
                "ftbquests_count": sum(1 for name in names if name.startswith(root + "config/ftbquests/") and not name.endswith("/")),
            }
        )
    return result


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
        "## Method Notes For CrawlerAgent",
        "",
        "- Build a source graph first: project indexes, MC百科/BBSMC/XyeBBS pages, author/community/server sites, docs, and release pages.",
        "- Cloud-drive pages are blocker evidence unless a no-login direct archive URL is visible and probeable.",
        "- For public/community sites, probe small objective metadata endpoints such as `/dw/get.txt`, `/download.txt`, `/version.txt`, and `/update.txt`.",
        "- Accept a package candidate only after objective facts are visible: URL, HTTP status, final URL, content type, content range or length, and zip/mrpack magic. The LLM decides relevance from those facts.",
        "",
        "## Downloaded Archives",
        "",
    ]
    if downloads:
        for item in downloads:
            lines.extend([f"- {item.get('path')}", f"  - source: {item.get('url')}", f"  - bytes: {item.get('bytes')}"])
            validation = item.get("validation")
            if isinstance(validation, dict):
                lines.extend(
                    [
                        f"  - zip_entries: {validation.get('entries')}",
                        f"  - has_manifest_json: {validation.get('has_manifest_json')}",
                        f"  - has_minecraft_version_instance: {validation.get('has_minecraft_version_instance')}",
                        f"  - mods_count: {validation.get('mods_count')}",
                        f"  - ftbquests_count: {validation.get('ftbquests_count')}",
                    ]
                )
    else:
        lines.append("- None")
    lines.extend(["", "## Archive Candidates", ""])
    if candidates:
        for item in candidates:
            lines.extend([f"- {item.get('filename') or item.get('project_title')}", f"  - url: {item.get('url')}", f"  - source: {item.get('source')}"])
            if item.get("discovery_method"):
                lines.append(f"  - discovery_method: {item.get('discovery_method')}")
            for step in item.get("method_steps") or []:
                lines.append(f"  - method_step: {step}")
            if item.get("probe_status"):
                lines.extend(
                    [
                        f"  - probe_status: {item.get('probe_status')}",
                        f"  - probe_content_type: {item.get('probe_content_type')}",
                        f"  - probe_content_range: {item.get('probe_content_range')}",
                        f"  - probe_final_url: {item.get('probe_final_url')}",
                        f"  - probe_magic: {item.get('probe_magic')}",
                        f"  - archive_magic: {item.get('archive_magic')}",
                    ]
                )
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


def write_download_evidence(run_dir: Path, query: str, downloads: list[dict[str, Any]]) -> list[Path]:
    paths: list[Path] = []
    for index, item in enumerate(downloads, start=1):
        filename = str(item.get("filename") or Path(str(item.get("path") or "")).name or f"archive_{index}.zip")
        path = run_dir / f"downloaded_archive_evidence_{index}.md"
        validation = item.get("validation") if isinstance(item.get("validation"), dict) else {}
        lines = [
            f"# Downloaded modpack archive evidence: {filename}",
            "",
            "<!-- source: modpack_download_evidence -->",
            "",
            "Objective facts for CrawlerAgent and MCagent/RAG. Tools record facts only; CrawlerAgent decides relevance.",
            "",
            f"- query: {query}",
            f"- filename: {filename}",
            f"- source_page_or_metadata_endpoint: {item.get('page_url') or ''}",
            f"- direct_archive_url: {item.get('url') or ''}",
            f"- final_probe_url: {item.get('probe_final_url') or ''}",
            f"- probe_status: {item.get('probe_status') or ''}",
            f"- probe_content_type: {item.get('probe_content_type') or ''}",
            f"- probe_content_range: {item.get('probe_content_range') or ''}",
            f"- probe_magic_hex: {item.get('probe_magic') or ''}",
            f"- archive_magic: {item.get('archive_magic') or ''}",
            f"- bytes: {item.get('bytes') or item.get('size') or ''}",
            f"- sha256: {item.get('sha256') or ''}",
            f"- local_archive_path: {item.get('path') or ''}",
            f"- zip_entries: {validation.get('entries') or ''}",
            f"- has_minecraft_version_instance: {validation.get('has_minecraft_version_instance')}",
            f"- instance_root: {validation.get('instance_root') or ''}",
            f"- mods_count: {validation.get('mods_count') or ''}",
            f"- ftbquests_count: {validation.get('ftbquests_count') or ''}",
            "",
            "## Discovery Method",
            "",
            f"- discovery_method: {item.get('discovery_method') or ''}",
        ]
        for step in item.get("method_steps") or []:
            lines.append(f"- method_step: {step}")
        path.write_text("\n".join(lines), encoding="utf-8")
        paths.append(path)
    return paths


def discover_and_download(dest_root: Path, archive_root: Path, query: str, limit: int, download: bool, max_bytes: int, user_agent: str) -> dict[str, Any]:
    run_dir = dest_root / "modpack_download" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    run_dir.mkdir(parents=True, exist_ok=True)
    archive_dir = archive_root / slugify(query, "modpack") / "pack_archive"
    modrinth_candidates, modrinth_errors = modrinth_archive_candidates(query, user_agent=user_agent, limit=limit)
    curseforge_candidates, curseforge_pages, curseforge_errors = curseforge_archive_candidates(query, user_agent=user_agent, limit=limit)
    bbsmc_candidates: list[dict[str, Any]] = []
    bbsmc_pages: list[dict[str, Any]] = []
    bbsmc_blockers: list[dict[str, Any]] = []
    bbsmc_errors: list[dict[str, Any]] = []
    mcmod_pages: list[dict[str, Any]] = []
    mcmod_errors: list[dict[str, Any]] = []
    official_pages: list[dict[str, Any]] = []
    official_errors: list[dict[str, Any]] = []
    discovery_pages: list[dict[str, Any]] = []
    discovery_errors: list[dict[str, Any]] = []
    site_pages: list[dict[str, Any]] = []
    site_errors: list[dict[str, Any]] = []
    xye_pages: list[dict[str, Any]] = []
    xye_errors: list[dict[str, Any]] = []
    yuque_pages: list[dict[str, Any]] = []
    yuque_errors: list[dict[str, Any]] = []
    cloud_blockers: list[dict[str, Any]] = []
    cloud_errors: list[dict[str, Any]] = []
    release_candidates: list[dict[str, Any]] = []
    release_pages: list[dict[str, Any]] = []
    release_errors: list[dict[str, Any]] = []
    if not (modrinth_candidates or curseforge_candidates):
        bbsmc_candidates, bbsmc_pages, bbsmc_blockers, bbsmc_errors = bbsmc_archive_candidates(query, user_agent=user_agent, limit=limit)
        mcmod_pages, mcmod_errors = mcmod_external_pages(query, user_agent=user_agent, limit=limit)
        official_pages, official_errors = discover_official_site_pages(query, user_agent=user_agent, limit=limit)
        discovery_pages, discovery_errors = discover_public_pages(query, user_agent=user_agent, limit=limit)
        seed_context_pages = prioritize_source_pages(mcmod_pages + official_pages + discovery_pages, limit=max(limit * 2, 12))
        site_pages, site_errors = collect_public_site_pages(" ".join([query] + [str(page.get("url") or "") for page in seed_context_pages]), user_agent=user_agent, limit=limit)
        xye_pages, xye_errors = xye_release_pages(mcmod_pages + site_pages, user_agent=user_agent, limit=limit)
        yuque_pages, yuque_errors = yuque_doc_pages(query, user_agent=user_agent, limit=limit)
        cloud_blockers, cloud_errors = cloud_drive_observations(bbsmc_pages + mcmod_pages + site_pages + xye_pages + yuque_pages, query, user_agent=user_agent, limit=limit)
        release_candidates, release_pages, release_errors = public_release_candidates(
            query, user_agent=user_agent, limit=limit, discovery_pages=prioritize_source_pages(mcmod_pages + official_pages + discovery_pages + site_pages + xye_pages, limit=max(limit * 2, 16))
    )
    web_candidates: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    web_errors: list[dict[str, Any]] = []
    if not (modrinth_candidates or curseforge_candidates or bbsmc_candidates or release_candidates):
        web_candidates, pages, web_errors = archive_link_candidates(query, user_agent=user_agent, limit=limit)
    candidates = prioritize_archive_candidates(modrinth_candidates + curseforge_candidates + bbsmc_candidates + release_candidates + web_candidates, query)
    blockers = bbsmc_blockers + cloud_blockers
    pages = curseforge_pages + bbsmc_pages + mcmod_pages + official_pages + discovery_pages + site_pages + xye_pages + yuque_pages + release_pages + pages
    errors = modrinth_errors + curseforge_errors + bbsmc_errors + mcmod_errors + official_errors + discovery_errors + site_errors + xye_errors + yuque_errors + cloud_errors + release_errors + web_errors
    downloads: list[dict[str, Any]] = []
    if download:
        for candidate in candidates[:limit]:
            if not archive_candidate_matches_target(candidate, query):
                errors.append(
                    {
                        "stage": "download_skip_target_name_mismatch",
                        "url": candidate.get("url"),
                        "project_title": candidate.get("project_title"),
                        "project_slug": candidate.get("project_slug"),
                        "filename": candidate.get("filename"),
                        "query_terms": target_name_terms(query),
                        "reason": "candidate archive is objectively downloadable, but its title/slug/filename does not contain the target name terms; CrawlerAgent should review or choose another source graph node.",
                    }
                )
                continue
            if not archive_candidate_is_downloadable(candidate):
                errors.append(
                    {
                        "stage": "download_skip_unverified",
                        "url": candidate.get("url"),
                        "reason": "candidate did not expose zip/mrpack magic in objective Range probe",
                        "probe_status": candidate.get("probe_status"),
                        "probe_content_type": candidate.get("probe_content_type"),
                        "probe_error": candidate.get("probe_error"),
                    }
                )
                continue
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
    evidence_paths = write_download_evidence(run_dir, query, downloads)
    records = [
        {
            "title": f"Modpack archive discovery for {query}",
            "url": "",
            "path": str(report),
            "chars": report.stat().st_size,
            "status": "new",
        }
    ]
    for evidence_path in evidence_paths:
        records.append(
            {
                "title": f"Downloaded archive evidence for {query}",
                "url": "",
                "path": str(evidence_path),
                "chars": evidence_path.stat().st_size,
                "status": "new",
            }
        )
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
    failure_reason = (
        "No public .mrpack/.zip archive was found or downloadable without login/payment/captcha restrictions. "
        + (f"Observed {len(blockers)} cloud-drive/client-only blocker(s)." if blockers else "")
    ).strip()
    if candidates and not downloads and not download:
        failure_reason = ""
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
        "failure_reason": "" if downloads else failure_reason,
        "next_action": "Run modpack_internal on the downloaded archive."
        if downloads
        else (
            "CrawlerAgent should decide whether to download the probed candidate archive."
            if candidates and not download
            else "CrawlerAgent should use browser/project pages or ask the user for an archive if no public package is available."
        ),
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
    parser.add_argument("--max-mb", type=int, default=2600)
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
