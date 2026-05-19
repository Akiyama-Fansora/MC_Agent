from __future__ import annotations

import argparse
from datetime import datetime
import io
import json
from pathlib import Path
import re
import time
from typing import Any
from urllib.parse import urlencode
import urllib.error
import urllib.request
import http.client
import zipfile

from crawl_ledger import append_ledger, content_hash, ledger_record, load_ledger, make_key, same_record_content


PROJECT_ROOT = Path(__file__).resolve().parents[1]
API_BASE = "https://api.modrinth.com/v2"
DEFAULT_USER_AGENT = "MC_Agent/0.1 (local RAG seed; D:/magic/MC_Agent)"


PROJECT_TYPE_LIMITS = {
    "mod": 80,
    "modpack": 25,
    "resourcepack": 15,
    "shader": 10,
}


def slugify(value: str, fallback: str = "project") -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-._")
    return cleaned[:80] or fallback


def request_json(
    path: str,
    params: dict[str, Any] | None = None,
    user_agent: str = DEFAULT_USER_AGENT,
    retries: int = 3,
) -> Any:
    query = f"?{urlencode(params)}" if params else ""
    request = urllib.request.Request(
        f"{API_BASE}{path}{query}",
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
                raise RuntimeError(f"Modrinth API HTTP {exc.code}: {detail}") from exc
            last_error = RuntimeError(f"Modrinth API HTTP {exc.code}: {detail}")
        except (urllib.error.URLError, TimeoutError, http.client.RemoteDisconnected) as exc:
            last_error = exc
        if attempt < retries:
            time.sleep(0.8 * (attempt + 1))
    raise RuntimeError(f"Modrinth API request failed after retries: {last_error}")


def request_bytes(url: str, user_agent: str = DEFAULT_USER_AGENT, retries: int = 3) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "*/*",
            "User-Agent": user_agent,
        },
    )
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, http.client.RemoteDisconnected) as exc:
            last_error = exc
        if attempt < retries:
            time.sleep(0.8 * (attempt + 1))
    raise RuntimeError(f"Download failed after retries: {last_error}")


def search_projects(project_type: str, limit: int, user_agent: str, query: str = "", offset: int = 0) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "facets": json.dumps([[f"project_type:{project_type}"]], separators=(",", ":")),
        "index": "downloads",
        "limit": min(limit, 100),
        "offset": max(0, offset),
    }
    if query:
        params["query"] = query
    data = request_json(
        "/search",
        params,
        user_agent=user_agent,
    )
    hits = data.get("hits", []) if isinstance(data, dict) else []
    return [item for item in hits if isinstance(item, dict)]


def resolve_projects(project_ids: list[str], user_agent: str) -> dict[str, dict[str, Any]]:
    resolved: dict[str, dict[str, Any]] = {}
    for index in range(0, len(project_ids), 100):
        batch = project_ids[index : index + 100]
        if not batch:
            continue
        try:
            data = request_json("/projects", {"ids": json.dumps(batch, separators=(",", ":"))}, user_agent=user_agent)
        except RuntimeError:
            continue
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("id"):
                    resolved[str(item["id"])] = item
    return resolved


def project_url(project: dict[str, Any]) -> str:
    project_type = str(project.get("project_type") or "mod")
    slug = str(project.get("slug") or project.get("id") or "")
    return f"https://modrinth.com/{project_type}/{slug}" if slug else "https://modrinth.com"


def list_text(values: Any, limit: int | None = None) -> str:
    if not isinstance(values, list):
        return ""
    items = [str(item) for item in values if str(item).strip()]
    if limit is not None:
        items = items[:limit]
    return ", ".join(items)


def license_text(value: Any) -> str:
    if isinstance(value, dict):
        name = value.get("name") or value.get("id") or ""
        url = value.get("url") or ""
        return f"{name} ({url})" if url else str(name)
    return str(value or "")


def _project_ids_from_downloads(downloads: Any) -> list[str]:
    ids: list[str] = []
    if not isinstance(downloads, list):
        return ids
    for url in downloads:
        match = re.search(r"/data/([^/]+)/versions/", str(url))
        if match:
            ids.append(match.group(1))
    return ids


def _simple_file_label(path: str) -> str:
    name = Path(path).name
    name = re.sub(r"\.(jar|zip|disabled)$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"[-_ ]?(fabric|forge|neoforge|quilt|mc)?[-_ ]?(\d+\.)+\d+.*$", "", name, flags=re.IGNORECASE)
    return name.strip("-_ .") or Path(path).name


def fetch_modpack_contents(project: dict[str, Any], user_agent: str) -> dict[str, Any] | None:
    project_id = str(project.get("id") or project.get("slug") or "").strip()
    if not project_id or str(project.get("project_type")) != "modpack":
        return None
    versions = request_json(f"/project/{project_id}/version", user_agent=user_agent)
    if not isinstance(versions, list) or not versions:
        return None
    version = next((item for item in versions if isinstance(item, dict)), None)
    if not version:
        return None
    files = version.get("files") if isinstance(version.get("files"), list) else []
    mrpack = next((item for item in files if isinstance(item, dict) and str(item.get("filename") or "").endswith(".mrpack")), None)
    if not mrpack and files:
        mrpack = next((item for item in files if isinstance(item, dict) and item.get("primary")), None)
    if not isinstance(mrpack, dict) or not mrpack.get("url"):
        return None

    archive = request_bytes(str(mrpack["url"]), user_agent=user_agent)
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        with zipped.open("modrinth.index.json") as fh:
            index = json.loads(fh.read().decode("utf-8"))

    file_entries = index.get("files") if isinstance(index.get("files"), list) else []
    project_ids: list[str] = []
    for file_entry in file_entries:
        if isinstance(file_entry, dict):
            project_ids.extend(_project_ids_from_downloads(file_entry.get("downloads")))
    project_ids = sorted(set(project_ids))
    resolved = resolve_projects(project_ids, user_agent)

    included: list[dict[str, Any]] = []
    for file_entry in file_entries:
        if not isinstance(file_entry, dict):
            continue
        downloads = file_entry.get("downloads")
        ids = _project_ids_from_downloads(downloads)
        project_ref = resolved.get(ids[0]) if ids else None
        included.append(
            {
                "path": file_entry.get("path"),
                "name": project_ref.get("title") if project_ref else _simple_file_label(str(file_entry.get("path") or "")),
                "project_id": ids[0] if ids else "",
                "project_slug": project_ref.get("slug") if project_ref else "",
                "project_type": project_ref.get("project_type") if project_ref else "",
                "downloads": downloads,
                "env": file_entry.get("env"),
                "file_size": file_entry.get("fileSize"),
            }
        )
    included.sort(key=lambda item: str(item.get("name") or "").lower())
    return {
        "version_id": version.get("id"),
        "version_name": version.get("name"),
        "version_number": version.get("version_number"),
        "game_versions": version.get("game_versions"),
        "loaders": version.get("loaders"),
        "mrpack_filename": mrpack.get("filename"),
        "index_name": index.get("name"),
        "index_version_id": index.get("versionId"),
        "dependencies": index.get("dependencies"),
        "included_files": included,
    }


def modpack_contents_to_markdown(contents: dict[str, Any]) -> list[str]:
    lines = [
        "",
        "## Modpack Contents",
        "",
        f"- **Version:** {contents.get('version_name') or contents.get('version_number') or contents.get('version_id')}",
        f"- **Mrpack file:** {contents.get('mrpack_filename')}",
    ]
    dependencies = contents.get("dependencies")
    if isinstance(dependencies, dict) and dependencies:
        lines.extend(["", "### Runtime Dependencies", ""])
        for key, value in sorted(dependencies.items()):
            lines.append(f"- **{key}:** {value}")

    included = contents.get("included_files") if isinstance(contents.get("included_files"), list) else []
    lines.extend(["", f"### Included Mods / Files ({len(included)})", ""])
    for item in included[:500]:
        name = item.get("name") or _simple_file_label(str(item.get("path") or ""))
        path = item.get("path") or ""
        project_id = item.get("project_id") or ""
        slug = item.get("project_slug") or ""
        suffix = f" ({slug or project_id})" if (slug or project_id) else ""
        lines.append(f"- {name}{suffix} — `{path}`")
    if len(included) > 500:
        lines.append(f"- ... {len(included) - 500} more files omitted from markdown preview.")
    return lines


def project_to_markdown(project: dict[str, Any], fetched_at: str, modpack_contents: dict[str, Any] | None = None) -> str:
    title = str(project.get("title") or project.get("slug") or project.get("id") or "Untitled")
    url = project_url(project)
    body = str(project.get("body") or "").strip()
    gallery = project.get("gallery") if isinstance(project.get("gallery"), list) else []
    gallery_lines = []
    for item in gallery[:8]:
        if not isinstance(item, dict):
            continue
        image_url = item.get("url")
        if not image_url:
            continue
        label = item.get("title") or item.get("description") or "image"
        gallery_lines.append(f"- {label}: {image_url}")

    fields = [
        ("Modrinth URL", url),
        ("Project ID", project.get("id")),
        ("Slug", project.get("slug")),
        ("Type", project.get("project_type")),
        ("Status", project.get("status")),
        ("Author/Team", project.get("team")),
        ("Description", project.get("description")),
        ("Client side", project.get("client_side")),
        ("Server side", project.get("server_side")),
        ("Downloads", project.get("downloads")),
        ("Followers", project.get("followers")),
        ("License", license_text(project.get("license"))),
        ("Loaders", list_text(project.get("loaders"))),
        ("Game versions", list_text(project.get("game_versions"), limit=60)),
        ("Categories", list_text(project.get("categories"))),
        ("Additional categories", list_text(project.get("additional_categories"))),
        ("Source", project.get("source_url")),
        ("Issues", project.get("issues_url")),
        ("Wiki", project.get("wiki_url")),
        ("Discord", project.get("discord_url")),
        ("Published", project.get("published")),
        ("Updated", project.get("updated")),
        ("Fetched at", fetched_at),
    ]

    lines = [
        f"# {title}",
        "",
        "<!-- source: modrinth_api -->",
        "",
        "## Metadata",
        "",
    ]
    for key, value in fields:
        if value is None or value == "":
            continue
        lines.append(f"- **{key}:** {value}")

    if gallery_lines:
        lines.extend(["", "## Gallery", "", *gallery_lines])

    lines.extend(["", "## Description", "", body or str(project.get("description") or "")])
    if modpack_contents:
        lines.extend(modpack_contents_to_markdown(modpack_contents))
    return "\n".join(lines).strip() + "\n"


def fetch_seed(
    dest_root: Path,
    limits: dict[str, int],
    user_agent: str,
    delay: float,
    query: str = "",
    force: bool = False,
    include_modpack_contents: bool = False,
    pages: int = 1,
) -> dict[str, Any]:
    run_dir = dest_root / "modrinth_agent" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    run_dir.mkdir(parents=True, exist_ok=True)
    fetched_at = datetime.now().isoformat(timespec="seconds")
    records: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    ledger = load_ledger()
    seen: set[str] = set()

    pages = max(1, min(int(pages), 50))
    for project_type, limit in limits.items():
        per_page = max(1, min(limit, 100))
        all_hits: list[dict[str, Any]] = []
        for page in range(pages):
            try:
                hits = search_projects(project_type, per_page, user_agent, query=query, offset=page * per_page)
            except RuntimeError as exc:
                errors.append({"stage": "search", "project_type": project_type, "page": page, "error": str(exc)})
                continue
            if not hits:
                break
            all_hits.extend(hits)
            time.sleep(delay)
        for hit in all_hits:
            slug_or_id = str(hit.get("slug") or hit.get("project_id") or "").strip()
            if not slug_or_id or slug_or_id in seen:
                continue
            seen.add(slug_or_id)
            try:
                project = request_json(f"/project/{slug_or_id}", user_agent=user_agent)
            except RuntimeError as exc:
                errors.append({"stage": "project", "slug": slug_or_id, "error": str(exc)})
                continue
            if not isinstance(project, dict):
                continue
            title = str(project.get("title") or slug_or_id)
            item_id = str(project.get("id") or project.get("slug") or slug_or_id)
            url = project_url(project)
            stable_text = json.dumps({"project": project, "modpack_contents": None}, ensure_ascii=False, sort_keys=True)
            key = make_key("modrinth", item_id)
            previous = ledger.get(key)
            if previous and not force:
                skipped.append(
                    {
                        "title": title,
                        "slug": project.get("slug"),
                        "project_type": project.get("project_type"),
                        "url": url,
                        "reason": "known_project",
                        "previous_path": previous.get("path", ""),
                    }
                )
                append_ledger(
                    ledger_record(
                        source="modrinth",
                        item_id=item_id,
                        title=title,
                        url=url,
                        text=stable_text,
                        path=str(previous.get("path", "")),
                        query=query,
                        status="skipped_unchanged",
                        previous=previous,
                    )
                )
                time.sleep(delay)
                continue
            modpack_contents: dict[str, Any] | None = None
            if include_modpack_contents and str(project.get("project_type")) == "modpack":
                try:
                    modpack_contents = fetch_modpack_contents(project, user_agent)
                except Exception as exc:  # noqa: BLE001 - project metadata is still useful if mrpack parsing fails.
                    errors.append({"stage": "modpack_contents", "slug": slug_or_id, "error": str(exc)})
            markdown = project_to_markdown(project, fetched_at, modpack_contents)
            stable_text = json.dumps({"project": project, "modpack_contents": modpack_contents}, ensure_ascii=False, sort_keys=True)
            digest = content_hash(stable_text)
            filename = f"{project_type}_{slugify(slug_or_id)}.md"
            path = run_dir / filename
            path.write_text(markdown, encoding="utf-8")
            status = "updated" if previous else "new"
            append_ledger(
                ledger_record(
                    source="modrinth",
                    item_id=item_id,
                title=title,
                url=url,
                text=stable_text,
                path=str(path),
                    query=query,
                    status=status,
                    previous=previous,
                )
            )
            records.append(
                {
                    "title": title,
                    "slug": project.get("slug"),
                    "project_type": project.get("project_type"),
                    "url": url,
                    "path": str(path),
                    "downloads": project.get("downloads"),
                    "updated": project.get("updated"),
                    "status": status,
                }
            )
            time.sleep(delay)

    manifest = {
        "manifest_type": "modrinth_seed_export",
        "created_at": fetched_at,
        "api_base": API_BASE,
        "user_agent": user_agent,
        "export_dir": str(run_dir),
        "query": query,
        "limits": limits,
        "pages": pages,
        "records": records,
        "skipped": skipped,
        "errors": errors,
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fetch a small public Modrinth dataset as Markdown for MCagent RAG.")
    parser.add_argument("--dest", default=str(PROJECT_ROOT / "data" / "crawler_exports"))
    parser.add_argument("--mods", type=int, default=PROJECT_TYPE_LIMITS["mod"])
    parser.add_argument("--modpacks", type=int, default=PROJECT_TYPE_LIMITS["modpack"])
    parser.add_argument("--resourcepacks", type=int, default=PROJECT_TYPE_LIMITS["resourcepack"])
    parser.add_argument("--shaders", type=int, default=PROJECT_TYPE_LIMITS["shader"])
    parser.add_argument("--query", default="", help="Optional Modrinth search query from the MCagent task.")
    parser.add_argument("--delay", type=float, default=0.12, help="Delay between project detail requests.")
    parser.add_argument("--force", action="store_true", help="Write files even if the ledger says content is unchanged.")
    parser.add_argument("--include-modpack-contents", action="store_true", help="Download .mrpack files and extract included mod/file lists for modpack results.")
    parser.add_argument("--pages", type=int, default=1, help="Fetch additional Modrinth search result pages per project type.")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    limits = {
        "mod": max(0, min(args.mods, 100)),
        "modpack": max(0, min(args.modpacks, 100)),
        "resourcepack": max(0, min(args.resourcepacks, 100)),
        "shader": max(0, min(args.shaders, 100)),
    }
    manifest = fetch_seed(
        Path(args.dest).resolve(),
        limits,
        args.user_agent,
        max(0.0, args.delay),
        query=args.query.strip(),
        force=args.force,
        include_modpack_contents=args.include_modpack_contents,
        pages=args.pages,
    )
    print(f"Exported to: {manifest['export_dir']}")
    print(f"Markdown files: {len(manifest['records'])}")
    print(f"Skipped unchanged: {len(manifest['skipped'])}")
    print(f"Errors: {len(manifest['errors'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
