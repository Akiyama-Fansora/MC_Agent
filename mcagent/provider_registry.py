from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from html import unescape
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Callable
from urllib.parse import urlparse, parse_qsl, urlencode, urlsplit, urlunsplit
import urllib.error
import urllib.request

from .cleaners import _HTMLTextExtractor, normalize_text


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(slots=True)
class ProviderCapability:
    search: bool = False
    extract: bool = False
    crawl: bool = False
    browser: bool = False


@dataclass(slots=True)
class ProviderSpec:
    id: str
    label: str
    capabilities: ProviderCapability
    requires_env: tuple[str, ...] = ()
    optional_env: tuple[str, ...] = ()
    default_limit: int = 8
    timeout_seconds: int = 900
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["configured"] = provider_configured(self)
        return data


@dataclass(slots=True)
class ProviderResult:
    provider: str
    stage: str
    query: str
    title: str
    url: str
    markdown: str
    raw_html: str = ""
    score: float = 0.0
    status: str = "ok"
    metadata: dict[str, Any] = field(default_factory=dict)


PROVIDERS: dict[str, ProviderSpec] = {
    "mcmod": ProviderSpec(
        id="mcmod",
        label="MC百科",
        capabilities=ProviderCapability(search=True, extract=True),
        default_limit=8,
        timeout_seconds=600,
        notes="中文 MC 模组、整合包、教程和资料页。",
    ),
    "modrinth": ProviderSpec(
        id="modrinth",
        label="Modrinth API",
        capabilities=ProviderCapability(search=True, extract=True),
        default_limit=60,
        timeout_seconds=600,
        notes="模组、整合包、资源包、光影和 .mrpack 清单。",
    ),
    "mediawiki": ProviderSpec(
        id="mediawiki",
        label="Minecraft Wiki API",
        capabilities=ProviderCapability(search=True, extract=True),
        default_limit=12,
        timeout_seconds=480,
        notes="原版 Minecraft 机制。",
    ),
    "ftbwiki": ProviderSpec(
        id="ftbwiki",
        label="FTB Wiki API",
        capabilities=ProviderCapability(search=True, extract=True),
        default_limit=12,
        timeout_seconds=480,
        notes="大型模组 Wiki。",
    ),
    "createwiki": ProviderSpec(
        id="createwiki",
        label="Create Wiki API",
        capabilities=ProviderCapability(search=True, extract=True),
        default_limit=12,
        timeout_seconds=480,
        notes="机械动力 Create 专门资料。",
    ),
    "followup": ProviderSpec(
        id="followup",
        label="公开项目文档跟进",
        capabilities=ProviderCapability(extract=True, crawl=True),
        default_limit=40,
        timeout_seconds=900,
        notes="从 Source/Wiki/README/官网链接继续抓取。",
    ),
    "mcagent_context": ProviderSpec(
        id="mcagent_context",
        label="MCagent/RAG Local Context",
        capabilities=ProviderCapability(search=True, extract=True),
        default_limit=10,
        timeout_seconds=120,
        notes="CrawlerAgent 向 MCagent 发送跨 Agent 消息；MCagent 使用自己的本地 RAG/证据流程后回复 CrawlerAgent。不是公网搜索，也不是 Crawler 直接读库。",
    ),
    "web_discovery": ProviderSpec(
        id="web_discovery",
        label="公开搜索发现",
        capabilities=ProviderCapability(search=True, extract=True),
        default_limit=8,
        timeout_seconds=900,
        notes="Bing RSS/HTML/GitHub 搜索兜底。",
    ),
    "playwright": ProviderSpec(
        id="playwright",
        label="Playwright Browser Search/Extract",
        capabilities=ProviderCapability(search=True, extract=True, browser=True),
        default_limit=6,
        timeout_seconds=900,
        notes="本地浏览器采集，适合搜索 API 额度不足、JS/表格/图片/下载页、需要 raw HTML 的页面。",
    ),
    "browser_collect": ProviderSpec(
        id="browser_collect",
        label="Browser Structured Collect",
        capabilities=ProviderCapability(search=True, extract=True, browser=True),
        default_limit=50,
        timeout_seconds=900,
        notes="通用结构化浏览器采集，按目标字段保存 XLSX/CSV/JSON/report/raw HTML/截图到指定目录；不会绕过登录、验证码或安全验证。",
    ),
    "fetch_url": ProviderSpec(
        id="fetch_url",
        label="Local URL Fetch/Extract",
        capabilities=ProviderCapability(extract=True),
        default_limit=1,
        timeout_seconds=180,
        notes="Generic no-key HTTP URL fetcher. Saves readable text, raw HTML, and manifest; useful before hosted reader APIs when the task already contains an exact public URL.",
    ),
    "save_artifact": ProviderSpec(
        id="save_artifact",
        label="Save Artifact",
        capabilities=ProviderCapability(extract=True),
        default_limit=1,
        timeout_seconds=120,
        notes="Generic local persistence tool for agent-provided content. Saves txt/md/json/jsonl/csv/html plus manifest; it does not fetch web pages or ingest by itself.",
    ),
    "read_local_file": ProviderSpec(
        id="read_local_file",
        label="Read Local File",
        capabilities=ProviderCapability(extract=True),
        default_limit=1,
        timeout_seconds=120,
        notes="Generic local file read tool. Converts one local text file into a Markdown artifact and manifest.",
    ),
    "search_local_files": ProviderSpec(
        id="search_local_files",
        label="Search Local Files",
        capabilities=ProviderCapability(search=True, extract=True),
        default_limit=25,
        timeout_seconds=180,
        notes="Generic local file search tool. Searches text files under a path and saves matching snippets to a report and manifest.",
    ),
    "modpack_download": ProviderSpec(
        id="modpack_download",
        label="Modpack Archive Discovery/Download",
        capabilities=ProviderCapability(search=True, extract=True, crawl=True),
        default_limit=8,
        timeout_seconds=1200,
        notes=(
            "Discover public .mrpack/.zip modpack archives and save them to local manual_research for modpack_internal. "
            "CrawlerAgent should use stable route order: Modrinth project_type:modpack/version files.url, CurseForge public/API file pages with visible direct downloadUrl, "
            "GitHub Releases assets/browser_download_url, packwiz pack.toml/index.toml repositories, then forum/community direct links. "
            "The provider reports objective candidates, HTTP/download facts, and blockers; CrawlerAgent decides relevance. "
            "It does not bypass login, payment, cloud-drive, client-only, or captcha restrictions."
        ),
    ),
}


def read_dotenv(path: Path | None = None) -> dict[str, str]:
    path = path or PROJECT_ROOT / ".env"
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        values[key.strip().lstrip("\ufeff")] = value.strip().strip('"').strip("'")
    return values


def env_value(name: str) -> str:
    return os.environ.get(name, "").strip() or read_dotenv().get(name, "").strip()


def provider_configured(provider: ProviderSpec) -> bool:
    return all(env_value(name) for name in provider.requires_env)


def providers_payload() -> list[dict[str, Any]]:
    return [provider.to_dict() for provider in PROVIDERS.values()]


def slugify(value: str, fallback: str = "page") -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-._")
    return cleaned[:90] or fallback


def unique_output_path(run_dir: Path, filename: str, digest: str) -> Path:
    path = run_dir / filename
    if not path.exists():
        return path
    return run_dir / f"{path.stem}_{digest[:8]}{path.suffix}"


TRACKING_QUERY_KEYS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "spm",
    "from",
    "share",
    "share_source",
    "timestamp",
}


def normalize_url_for_dedupe(url: str) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return text.lower().rstrip("/")
    scheme = (parts.scheme or "https").lower()
    host = parts.netloc.lower()
    if host.endswith(":80") and scheme == "http":
        host = host[:-3]
    if host.endswith(":443") and scheme == "https":
        host = host[:-4]
    path = re.sub(r"/+", "/", parts.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query_items = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_QUERY_KEYS and not key.lower().startswith("utm_")
    ]
    query = urlencode(sorted(query_items), doseq=True)
    return urlunsplit((scheme, host, path, query, ""))


def content_fingerprint_for_dedupe(text: str) -> str:
    import hashlib

    stable = str(text or "")
    stable = re.sub(r"<!--\s*source:\s*[^>]+-->", "", stable, flags=re.I)
    stable = re.sub(r"\n## Metadata\n.*?(?=\n## |\Z)", "\n", stable, flags=re.S)
    stable = re.sub(r"\n+- \*\*(?:Fetched at|Query|Search query|Provider|Stage|Score|URL|MC百科 URL|Web source|Search rank):\*\*.*", "", stable, flags=re.I)
    stable = re.sub(r"\s+", " ", stable).strip().lower()
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def global_dedupe_indexes(ledger: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    by_url: dict[str, dict[str, Any]] = {}
    by_content: dict[str, dict[str, Any]] = {}
    for value in ledger.values():
        if not isinstance(value, dict):
            continue
        status = str(value.get("status") or "")
        if status.startswith("skipped"):
            continue
        url = str(value.get("url") or "")
        if url:
            by_url.setdefault(str(value.get("url_key") or f"global_url:{normalize_url_for_dedupe(url)}"), value)
        fingerprint = str(value.get("content_fingerprint") or "")
        if fingerprint:
            by_content.setdefault(f"global_content:{fingerprint}", value)
        elif value.get("content_hash"):
            by_content.setdefault(f"global_content_hash:{value.get('content_hash')}", value)
    return by_url, by_content


def previous_content_fingerprint(record: dict[str, Any] | None) -> str:
    if not record:
        return ""
    fingerprint = str(record.get("content_fingerprint") or "")
    if fingerprint:
        return fingerprint
    path = Path(str(record.get("path") or ""))
    if path.exists() and path.is_file():
        try:
            return content_fingerprint_for_dedupe(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return ""
    return ""


def request_text(url: str, user_agent: str, timeout: int = 30, retries: int = 1) -> tuple[str, str, int]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "text/html,text/plain,application/json,*/*",
            "User-Agent": user_agent,
        },
    )
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                content_type = response.headers.get("Content-Type", "")
                charset = response.headers.get_content_charset() or "utf-8"
                return raw.decode(charset, errors="replace"), content_type, int(response.status)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
        if attempt < retries:
            time.sleep(0.7 * (attempt + 1))
    raise RuntimeError(f"Failed to fetch {url}: {last_error}")


def request_json(
    url: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            **(headers or {}),
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
        return json.loads(raw.decode(charset, errors="replace"))


def query_tokens(query: str) -> list[str]:
    stop = {
        "minecraft",
        "mc",
        "mod",
        "mods",
        "玩法",
        "介绍",
        "详细",
        "哪些",
        "什么",
        "怎么",
        "如何",
        "合成",
        "配方",
    }
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_+-]{1,}|[\u4e00-\u9fff]{2,}", query.lower())
    return list(dict.fromkeys(token for token in tokens if token not in stop))


def query_variants(query: str) -> list[str]:
    base = normalize_text(query).strip()
    variants = [base]
    tokens = query_tokens(base)
    if tokens:
        variants.append(" ".join(tokens[:5]))
    if "minecraft" not in base.lower():
        variants.append(f"{base} Minecraft")
    if "mc百科" not in base and "mcmod" not in base.lower():
        variants.append(f"{base} MC百科")
    if "mod" not in base.lower():
        variants.append(f"{base} Minecraft mod")
    return list(dict.fromkeys(item for item in variants if item.strip()))[:8]


def relevance_score(query: str, title: str, snippet: str, text: str = "", url: str = "") -> float:
    terms = query_tokens(query)
    if not terms:
        return 0.2
    haystack = f"{title}\n{snippet}\n{url}\n{text[:9000]}".lower()
    hits = sum(1 for term in terms if term.lower() in haystack)
    phrase_bonus = 1.0 if query.strip().lower() in haystack else 0.0
    mc_bonus = 0.4 if any(mark in haystack for mark in ("minecraft", "mc百科", "mcmod", "modrinth", "wiki", "forge", "fabric")) else 0.0
    return hits / max(1, len(terms)) + phrase_bonus + mc_bonus


def tables_to_markdown(tables: list[list[list[str]]]) -> str:
    chunks: list[str] = []
    for table_index, rows in enumerate(tables, start=1):
        cleaned_rows = [[normalize_text(cell) for cell in row] for row in rows if any(normalize_text(cell) for cell in row)]
        if not cleaned_rows:
            continue
        width = max(len(row) for row in cleaned_rows)
        normalized = [row + [""] * (width - len(row)) for row in cleaned_rows]
        chunks.append(f"### Table {table_index}")
        chunks.append("| " + " | ".join(normalized[0]) + " |")
        chunks.append("| " + " | ".join(["---"] * width) + " |")
        for row in normalized[1:]:
            chunks.append("| " + " | ".join(row) + " |")
        chunks.append("")
    return "\n".join(chunks).strip()


def images_to_markdown(images: list[dict[str, str]], base_url: str) -> str:
    rows: list[str] = []
    for image in images[:80]:
        src = image.get("src", "").strip()
        if not src:
            continue
        if src.startswith("//"):
            src = "https:" + src
        elif src.startswith("/"):
            parsed = urlparse(base_url)
            src = f"{parsed.scheme}://{parsed.netloc}{src}"
        alt = normalize_text(image.get("alt", ""))
        rows.append(f"- image: {src}" + (f" alt={alt}" if alt else ""))
    return "\n".join(rows)


def extract_tables_images(content: str, url: str) -> tuple[str, str, str]:
    if not re.search(r"<html|<body|<article|<main|<table|<img", content[:4000], flags=re.I):
        return normalize_text(unescape(content)), "", ""
    parser = _HTMLTextExtractor()
    parser.feed(content)
    parser.close()
    return parser.text, tables_to_markdown(parser.tables), images_to_markdown(parser.images, url)


def result_to_markdown(result: ProviderResult) -> tuple[str, str]:
    text, tables, images = extract_tables_images(result.markdown, result.url)
    lines = [
        f"# {normalize_text(result.title) or result.url}",
        "",
        f"<!-- source: {result.provider} -->",
        "",
        "## Metadata",
        "",
        f"- **URL:** {result.url}",
        f"- **Provider:** {result.provider}",
        f"- **Stage:** {result.stage}",
        f"- **Query:** {result.query}",
        f"- **Score:** {round(float(result.score or 0.0), 3)}",
    ]
    for key, value in result.metadata.items():
        if value in (None, ""):
            continue
        lines.append(f"- **{key}:** {value}")
    lines.extend(["", "## Content", "", normalize_text(text)])
    if tables:
        lines.extend(["", "## Extracted Tables", "", tables])
    if images:
        lines.extend(["", "## Images", "", images])
    return result.title, "\n".join(lines).strip() + "\n"


def export_provider_results(
    *,
    dest_root: Path,
    provider: str,
    query: str,
    results: list[ProviderResult],
    search_results: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
    errors: list[dict[str, str]],
    force: bool,
    content_hash: Callable[[str], str],
    ledger_record: Callable[..., dict[str, Any]],
    append_ledger: Callable[[dict[str, Any]], None],
    load_ledger: Callable[[], dict[str, Any]],
    make_key: Callable[[str, str], str],
    extra_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    run_dir = dest_root / provider / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    run_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = run_dir / "raw_html"
    raw_dir.mkdir(parents=True, exist_ok=True)
    fetched_at = datetime.now().isoformat(timespec="seconds")
    ledger = load_ledger()
    global_urls, global_contents = global_dedupe_indexes(ledger)
    records: list[dict[str, Any]] = []

    for result in results:
        title, markdown = result_to_markdown(result)
        digest = content_hash(markdown)
        item_id = result.url.lower().rstrip("/")
        key = make_key(provider, item_id)
        previous = ledger.get(key)
        url_key = f"global_url:{normalize_url_for_dedupe(result.url)}"
        content_key = f"global_content:{content_fingerprint_for_dedupe(markdown)}"
        global_previous = global_urls.get(url_key) or global_contents.get(content_key)
        if global_previous and not force:
            skipped.append(
                {
                    "url": result.url,
                    "reason": "url_or_content_duplicate",
                    "previous_source": global_previous.get("source", ""),
                    "previous_path": global_previous.get("path", ""),
                    "score": round(result.score, 3),
                }
            )
            append_ledger(
                ledger_record(
                    source=provider,
                    item_id=item_id,
                    title=title,
                    url=result.url,
                    text=markdown,
                    path=str(global_previous.get("path", "")),
                    query=query,
                    status="skipped_global_duplicate",
                    previous=global_previous,
                )
            )
            continue
        if previous and (previous.get("content_hash") == digest or previous_content_fingerprint(previous) == content_key.removeprefix("global_content:")) and not force:
            skipped.append({"url": result.url, "reason": "unchanged", "previous_path": previous.get("path", ""), "score": round(result.score, 3)})
            append_ledger(
                ledger_record(
                    source=provider,
                    item_id=item_id,
                    title=title,
                    url=result.url,
                    text=markdown,
                    path=str(previous.get("path", "")),
                    query=query,
                    status="skipped_unchanged",
                    previous=previous,
                )
            )
            continue
        path = unique_output_path(run_dir, f"{provider}_{slugify(title, 'page')}.md", digest)
        raw_path = raw_dir / f"{path.stem}.html"
        path.write_text(markdown, encoding="utf-8")
        if result.raw_html:
            raw_path.write_text(result.raw_html, encoding="utf-8")
        record = ledger_record(
            source=provider,
            item_id=item_id,
            title=title,
            url=result.url,
            text=markdown,
            path=str(path),
            query=query,
            status="updated" if previous else "new",
            previous=previous,
        )
        append_ledger(record)
        global_urls[url_key] = record
        global_contents[content_key] = record
        records.append(
            {
                "title": title,
                "url": result.url,
                "path": str(path),
                "raw_html_path": str(raw_path) if result.raw_html else "",
                "chars": len(markdown),
                "raw_html_chars": len(result.raw_html),
                "score": round(result.score, 3),
                "provider": provider,
                "stage": result.stage,
                "status": result.status,
                "metadata": result.metadata,
            }
        )

    manifest = {
        "manifest_type": f"{provider}_export",
        "created_at": fetched_at,
        "export_dir": str(run_dir),
        "query": query,
        "provider": provider,
        "provider_schema_version": 1,
        "provider_spec": PROVIDERS.get(provider).to_dict() if provider in PROVIDERS else {},
        "search_results": search_results[:120],
        "records": records,
        "skipped": skipped,
        "errors": errors,
        **(extra_manifest or {}),
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest
