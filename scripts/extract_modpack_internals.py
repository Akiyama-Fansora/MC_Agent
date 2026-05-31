from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
import zipfile


TEXT_EXTS = {
    ".json",
    ".json5",
    ".snbt",
    ".js",
    ".toml",
    ".txt",
    ".properties",
    ".mcmeta",
    ".cfg",
    ".lua",
    ".yaml",
    ".yml",
    ".ini",
    ".html",
}


class ModListParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[dict[str, str]] = []
        self._href = ""
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            self._href = dict(attrs).get("href") or ""
            self._buf = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href:
            text = "".join(self._buf).strip()
            if text:
                self.links.append({"name": text, "url": self._href})
            self._href = ""
            self._buf = []


def decode_bytes(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def safe_name(path: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", path)
    return cleaned.strip("_")[:180] or "file"


def strip_color(text: str) -> str:
    return re.sub(r"&[0-9a-fk-orA-FK-OR]", "", text)


def string_values(text: str, key: str) -> list[str]:
    values = []
    pattern = re.compile(rf"\b{re.escape(key)}\s*:\s*\"((?:\\.|[^\"])*)\"", re.S)
    for match in pattern.finditer(text):
        value = match.group(1).replace('\\"', '"').replace("\\n", "\n")
        values.append(strip_color(value))
    return values


def list_values(text: str, key: str) -> list[str]:
    values: list[str] = []
    pattern = re.compile(rf"\b{re.escape(key)}\s*:\s*\[(.*?)\]", re.S)
    for block in pattern.findall(text):
        for item in re.findall(r"\"((?:\\.|[^\"])*)\"", block):
            values.append(strip_color(item.replace('\\"', '"').replace("\\n", "\n")))
    return values


def compact(value: str, limit: int = 260) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) > limit:
        return value[: limit - 1] + "..."
    return value


def read_zip_text(zf: zipfile.ZipFile, name: str) -> str:
    return decode_bytes(zf.read(name))


def parse_recipe_json(text: str, name: str) -> dict[str, object] | None:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    result = data.get("result")
    result_item = ""
    if isinstance(result, dict):
        result_item = str(result.get("item") or result.get("id") or "")
    elif isinstance(result, str):
        result_item = result
    blade = str(data.get("blade") or "")
    key_items: list[str] = []
    key = data.get("key")
    if isinstance(key, dict):
        for entry in key.values():
            if isinstance(entry, dict):
                item = entry.get("item") or entry.get("tag") or entry.get("name")
                if item:
                    key_items.append(str(item))
                request = entry.get("request")
                if isinstance(request, dict):
                    req = ", ".join(f"{k}={v}" for k, v in request.items())
                    key_items.append(f"request({req})")
    return {
        "path": name,
        "type": data.get("type"),
        "blade": blade,
        "result": result_item,
        "pattern": data.get("pattern") if isinstance(data.get("pattern"), list) else [],
        "ingredients": key_items,
    }


def parse_named_blade(text: str, name: str) -> dict[str, object] | None:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return {
        "path": name,
        "name": data.get("name") or data.get("translation_key") or Path(name).stem,
        "properties": {k: data.get(k) for k in ("attack", "max_damage", "rarity", "soul", "model", "texture") if k in data},
    }


def write_markdown(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def detect_pack_layout(names: list[str]) -> dict[str, object]:
    if "manifest.json" in names and "modlist.html" in names:
        return {"kind": "curseforge", "root": "", "pack_id": "curseforge"}
    instance_dirs = [name for name in names if name.startswith(".minecraft/versions/") and name.endswith("/") and name.count("/") == 3]
    if instance_dirs:
        root = instance_dirs[0]
        return {"kind": "minecraft_instance", "root": root, "pack_id": Path(root.rstrip("/")).name}
    return {"kind": "unknown", "root": "", "pack_id": "modpack"}


def pack_path(root: str, suffix: str) -> str:
    if not root:
        return suffix
    return root + suffix.removeprefix("/")


def extract(zip_path: Path, export_dir: Path, manual_dir: Path) -> dict[str, object]:
    run_dir = export_dir / "manual_research" / f"{datetime.now():%Y%m%d_%H%M%S}_{safe_name(zip_path.stem)}_pack_internals"
    raw_dir = run_dir / "raw_text"
    raw_dir.mkdir(parents=True, exist_ok=True)
    manual_dir.mkdir(parents=True, exist_ok=True)

    records: list[dict[str, object]] = []
    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()
        names = [info.filename for info in infos]
        layout = detect_pack_layout(names)
        layout_kind = str(layout.get("kind") or "unknown")
        instance_root = str(layout.get("root") or "")
        pack_id = str(layout.get("pack_id") or zip_path.stem)
        ext_counter = Counter(Path(name).suffix.lower() or "[noext]" for name in names)
        folder_counter = Counter("/".join(name.split("/")[:2]) for name in names if "/" in name)

        manifest: dict[str, object] = {}
        if "manifest.json" in names:
            manifest = json.loads(read_zip_text(zf, "manifest.json"))
        mod_parser = ModListParser()
        if "modlist.html" in names:
            mod_parser.feed(read_zip_text(zf, "modlist.html"))
        instance_json_name = pack_path(instance_root, f"{pack_id}.json") if instance_root else ""
        instance_json: dict[str, object] = {}
        if instance_json_name in names:
            try:
                instance_json = json.loads(read_zip_text(zf, instance_json_name))
            except json.JSONDecodeError:
                instance_json = {}
        if layout_kind == "curseforge":
            pack_id = str(manifest.get("name") or zip_path.stem)

        text_names = []
        for info in infos:
            name = info.filename
            if info.is_dir():
                continue
            ext = Path(name).suffix.lower()
            if ext not in TEXT_EXTS:
                continue
            if info.file_size > 2_500_000:
                continue
            interesting = (
                name in {"manifest.json", "modlist.html", instance_json_name}
                or name.startswith(pack_path(instance_root, "config/ftbquests/"))
                or name.startswith(pack_path(instance_root, "kubejs/"))
                or name.startswith(pack_path(instance_root, "config/openloader/"))
                or name.startswith(pack_path(instance_root, "tacz/"))
                or name.startswith(pack_path(instance_root, "defaultconfigs/"))
                or name.startswith(pack_path(instance_root, "config/"))
            )
            if interesting:
                text_names.append(name)

        for name in text_names:
            target = raw_dir / f"{safe_name(name)}.txt"
            target.write_text(read_zip_text(zf, name), encoding="utf-8", errors="replace")

        quest_files = [n for n in text_names if n.startswith(pack_path(instance_root, "config/ftbquests/quests/chapters/"))]
        quest_rows = []
        for name in quest_files:
            text = read_zip_text(zf, name)
            titles = string_values(text, "title")
            subtitles = string_values(text, "subtitle")
            descriptions = list_values(text, "description")
            items = re.findall(r'\bitem:\s*"([^"]+)"', text)
            quest_rows.append(
                {
                    "path": name,
                    "chapter_title": titles[-1] if titles else Path(name).stem,
                    "title_count": len(titles),
                    "sample_titles": titles[:40],
                    "sample_subtitles": subtitles[:30],
                    "sample_descriptions": descriptions[:40],
                    "sample_items": list(dict.fromkeys(items))[:80],
                }
            )

        js_files = [n for n in text_names if n.startswith(pack_path(instance_root, "kubejs/")) and n.endswith(".js")]
        js_rows = []
        for name in js_files:
            text = read_zip_text(zf, name)
            shaped = re.findall(r"event\.shaped\(\s*['\"]([^'\"]+)['\"]", text)
            removed = re.findall(r"event\.remove\(\s*\{([^}]+)\}", text)
            mentioned = sorted(set(re.findall(r"['\"]([a-z0-9_]+:[a-z0-9_./-]+)['\"]", text, re.I)))
            js_rows.append(
                {
                    "path": name,
                    "chars": len(text),
                    "shaped_outputs": shaped[:120],
                    "remove_rules": [compact(x, 160) for x in removed[:80]],
                    "mentioned_ids": mentioned[:160],
                }
            )

        recipe_rows = []
        blade_rows = []
        for name in text_names:
            low = name.lower()
            if "/recipes/" in low and name.endswith(".json"):
                parsed = parse_recipe_json(read_zip_text(zf, name), name)
                if parsed:
                    recipe_rows.append(parsed)
            if "/slashblade/named_blades/" in low and name.endswith(".json"):
                parsed = parse_named_blade(read_zip_text(zf, name), name)
                if parsed:
                    blade_rows.append(parsed)

        keyword_terms = [
            "???",
            "??",
            "??",
            "??",
            "??",
            "Boss",
            "boss",
            "BOSS",
            "dragon",
            "Dragon",
            "slashblade",
            "yakumoblade",
            "tarot",
            "??",
            "tacz",
            "?",
            "????",
            "???",
            "farseer",
            "goety",
            "cataclysm",
            "mowzie",
            "BOMD",
            "????",
            "??",
            "??",
            "??",
            "??",
            "??",
            "??",
        ]
        keyword_pattern = re.compile("|".join(re.escape(term) for term in keyword_terms), re.I)
        keyword_hits = []
        for name in text_names:
            text = read_zip_text(zf, name)
            matches = []
            for i, line in enumerate(text.splitlines(), 1):
                if keyword_pattern.search(line):
                    matches.append({"line": i, "text": compact(line, 320)})
                if len(matches) >= 20:
                    break
            if matches:
                keyword_hits.append({"path": name, "matches": matches, "match_count": len(matches)})

        summary_lines = [
            f"# {pack_id} pack internal inventory",
            "",
            f"- generated_at: {datetime.now().isoformat(timespec='seconds')}",
            f"- archive: {zip_path}",
            f"- layout: {layout_kind}",
            f"- instance_root: {instance_root or '[archive root]'}",
            f"- zip_entries: {len(names)}",
            f"- extracted_text_files: {len(text_names)}",
            f"- pack_name: {manifest.get('name') or instance_json.get('id') or pack_id}",
            f"- pack_version: {manifest.get('version') or instance_json.get('releaseTime') or ''}",
            f"- minecraft: {manifest.get('minecraft', {}).get('version') if isinstance(manifest.get('minecraft'), dict) else instance_json.get('id')}",
            f"- mod_loader: {', '.join(x.get('id', '') for x in manifest.get('minecraft', {}).get('modLoaders', [])) if isinstance(manifest.get('minecraft'), dict) else ''}",
            f"- curseforge_files: {len(manifest.get('files', [])) if isinstance(manifest.get('files'), list) else 0}",
            f"- modlist_entries: {len(mod_parser.links)}",
            f"- jar_mod_files: {sum(1 for name in names if name.startswith(pack_path(instance_root, 'mods/')) and name.lower().endswith('.jar'))}",
            "",
            "## Top folders",
            "",
        ]
        summary_lines.extend(f"- {folder}: {count}" for folder, count in folder_counter.most_common(40))
        summary_lines.extend(["", "## Text extension counts", ""])
        summary_lines.extend(f"- {ext}: {count}" for ext, count in ext_counter.most_common(40))
        summary_lines.extend(["", "## Modlist sample", ""])
        summary_lines.extend(f"- {m['name']} - {m['url']}" for m in mod_parser.links[:160])
        mod_files = [n for n in names if n.startswith(pack_path(instance_root, "mods/")) and n.lower().endswith(".jar")]
        if mod_files:
            summary_lines.extend(["", "## Jar mod files", ""])
            summary_lines.extend(f"- {Path(name).name}" for name in mod_files[:500])
        inventory_path = run_dir / f"{safe_name(pack_id)}_pack_internal_inventory.md"
        write_markdown(inventory_path, summary_lines)

        quest_lines = [
            f"# {pack_id} FTB quest internal extraction",
            "",
            "This document is extracted from the pack archive FTB Quests files. It is evidence for RAG, not a final answer template.",
            "",
            f"- chapter_files: {len(quest_rows)}",
            "",
        ]
        for row in quest_rows:
            quest_lines.extend([f"## {row['chapter_title']}", "", f"- source: {row['path']}", f"- title_count: {row['title_count']}", ""])
            if row["sample_titles"]:
                quest_lines.append("### Quest titles")
                quest_lines.extend(f"- {compact(x, 180)}" for x in row["sample_titles"])
                quest_lines.append("")
            if row["sample_subtitles"]:
                quest_lines.append("### Subtitles")
                quest_lines.extend(f"- {compact(x, 220)}" for x in row["sample_subtitles"])
                quest_lines.append("")
            if row["sample_descriptions"]:
                quest_lines.append("### Description snippets")
                quest_lines.extend(f"- {compact(x, 260)}" for x in row["sample_descriptions"])
                quest_lines.append("")
            if row["sample_items"]:
                quest_lines.append("### Item IDs mentioned")
                quest_lines.extend(f"- {x}" for x in row["sample_items"][:60])
                quest_lines.append("")
        quest_path = run_dir / f"{safe_name(pack_id)}_ftbquests_extracted.md"
        write_markdown(quest_path, quest_lines)

        js_lines = [
            f"# {pack_id} KubeJS recipe and rule extraction",
            "",
            f"- script_files: {len(js_rows)}",
            "",
        ]
        for row in js_rows:
            js_lines.extend([f"## {row['path']}", "", f"- chars: {row['chars']}", ""])
            if row["shaped_outputs"]:
                js_lines.append("### shaped outputs")
                js_lines.extend(f"- {x}" for x in row["shaped_outputs"])
                js_lines.append("")
            if row["remove_rules"]:
                js_lines.append("### remove rules")
                js_lines.extend(f"- {x}" for x in row["remove_rules"])
                js_lines.append("")
            if row["mentioned_ids"]:
                js_lines.append("### mentioned ids")
                js_lines.extend(f"- {x}" for x in row["mentioned_ids"][:120])
                js_lines.append("")
        js_path = run_dir / f"{safe_name(pack_id)}_kubejs_extracted.md"
        write_markdown(js_path, js_lines)

        recipe_lines = [
            f"# {pack_id} OpenLoader recipes and SlashBlade data",
            "",
            f"- recipe_json_files: {len(recipe_rows)}",
            f"- slashblade_named_blades: {len(blade_rows)}",
            "",
            "## SlashBlade named blades",
            "",
        ]
        for row in blade_rows:
            props = row["properties"]
            recipe_lines.append(f"- {row['name']} | source: {row['path']} | props: {json.dumps(props, ensure_ascii=False)}")
        recipe_lines.extend(["", "## Recipe files", ""])
        for row in recipe_rows:
            ingredients = ", ".join(str(x) for x in row["ingredients"][:20])
            pattern = " / ".join(str(x) for x in row["pattern"])
            recipe_lines.append(f"- source: {row['path']} | type: {row['type']} | blade: {row['blade']} | result: {row['result']} | pattern: {pattern} | ingredients: {ingredients}")
        recipe_path = run_dir / f"{safe_name(pack_id)}_openloader_recipes_extracted.md"
        write_markdown(recipe_path, recipe_lines)

        keyword_lines = [
            f"# {pack_id} internal keyword evidence map",
            "",
            "This document lists objective keyword hits from internal pack files for bosses, routes, recipes, guns, and core systems.",
            "",
            f"- files_with_hits: {len(keyword_hits)}",
            "",
        ]
        for item in keyword_hits[:260]:
            keyword_lines.extend([f"## {item['path']}", "", f"- match_count: {item['match_count']}", ""])
            keyword_lines.extend(f"- L{m['line']}: {m['text']}" for m in item["matches"])
            keyword_lines.append("")
        keyword_path = run_dir / f"{safe_name(pack_id)}_internal_keyword_map.md"
        write_markdown(keyword_path, keyword_lines)

        guide_path = run_dir / f"{safe_name(pack_id)}_gameplay_route_index.md"
        write_markdown(guide_path, gameplay_route_lines(pack_id, zip_path, quest_rows, layout))

        generated = [inventory_path, quest_path, js_path, recipe_path, keyword_path, guide_path]
        for path in generated:
            shutil.copy2(path, manual_dir / path.name)
            records.append({"title": path.stem, "path": str(path), "url": f"local://{pack_id}_pack", "chars": path.stat().st_size})

        data = {
            "manifest_type": "modpack_internal_export",
            "source": "local_zip_archive",
            "archive": str(zip_path),
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "export_dir": str(run_dir),
            "layout": layout,
            "stats": {
                "zip_entries": len(names),
                "text_files_extracted": len(text_names),
                "quest_chapter_files": len(quest_rows),
                "kubejs_scripts": len(js_rows),
                "recipe_json_files": len(recipe_rows),
                "slashblade_named_blades": len(blade_rows),
                "keyword_hit_files": len(keyword_hits),
            },
            "records": records,
        }
        (run_dir / "manifest.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        (manual_dir / f"{safe_name(pack_id)}_pack_internal_manifest.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return data


def gameplay_route_lines(pack_id: str, zip_path: Path, quest_rows: list[dict[str, object]], layout: dict[str, object]) -> list[str]:
    aliases = [pack_id, zip_path.stem]
    if "乌托邦" in pack_id or "WuTuoBang" in zip_path.stem:
        aliases.extend(["乌托邦探险之旅", "乌托邦之旅", "Utopian Journey"])
    alias_text = "；".join(dict.fromkeys(alias for alias in aliases if alias))
    focus_words = (
        "萌新",
        "新手",
        "入门",
        "教程",
        "开局",
        "路线",
        "旅途",
        "主线",
        "指引",
        "导航",
        "必看",
        "机制",
        "职业",
        "装备",
        "BOSS",
        "Boss",
        "boss",
        "商店",
        "收集",
        "毕业",
    )
    rows = list(quest_rows)
    priority_rows: list[dict[str, object]] = []
    for row in rows:
        text = "\n".join(
            [
                str(row.get("chapter_title") or ""),
                "\n".join(str(item) for item in list(row.get("sample_titles") or [])[:80]),
                "\n".join(str(item) for item in list(row.get("sample_descriptions") or [])[:80]),
                "\n".join(str(item) for item in list(row.get("sample_subtitles") or [])[:80]),
            ]
        )
        if any(word in text for word in focus_words):
            priority_rows.append(row)

    lines = [
        f"# {pack_id} gameplay route and quest index",
        "",
        "This is an extracted index from the modpack archive. It helps MCagent retrieve the right pack for beginner route, gameplay, FTB Quests, chapters, shops, equipment, bosses, and progression questions.",
        "",
        f"- aliases: {alias_text}",
        f"- archive: {zip_path}",
        f"- layout: {layout.get('kind')}",
        f"- instance_root: {layout.get('root')}",
        "- retrieval_terms: 新手 萌新 开局 入门 路线 玩法 攻略 主线 任务线 FTB Quests 任务章节 导航 必看 机制 职业 装备 BOSS 商店 收集 毕业",
        "",
        "## All Quest Chapters",
        "",
    ]
    for row in rows:
        lines.append(f"- {row.get('chapter_title')} | source: {row.get('path')}")

    lines.extend(["", "## Beginner Route And Progression Signals", ""])
    for row in priority_rows:
        lines.extend([f"### {row.get('chapter_title')}", "", f"- source: {row.get('path')}"])
        titles = [str(item) for item in list(row.get("sample_titles") or [])[:24] if str(item).strip()]
        descriptions = [str(item) for item in list(row.get("sample_descriptions") or [])[:16] if str(item).strip()]
        subtitles = [str(item) for item in list(row.get("sample_subtitles") or [])[:12] if str(item).strip()]
        if titles:
            lines.append("- quest_titles: " + "；".join(compact(item, 80) for item in titles))
        if subtitles:
            lines.append("- subtitles: " + "；".join(compact(item, 100) for item in subtitles))
        if descriptions:
            lines.append("- description_snippets:")
            lines.extend(f"  - {compact(item, 220)}" for item in descriptions)
        lines.append("")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract RAG-readable text evidence from a Minecraft modpack archive.")
    parser.add_argument("--zip", required=True, help="Path to the modpack zip archive.")
    parser.add_argument("--export-dir", default=str(Path(__file__).resolve().parents[1] / "data" / "crawler_exports"))
    parser.add_argument("--manual-dir", default=str(Path(__file__).resolve().parents[1] / "data" / "manual_research" / "modpack_internals"))
    args = parser.parse_args()
    data = extract(Path(args.zip), Path(args.export_dir), Path(args.manual_dir))
    print(json.dumps({"export_dir": data["export_dir"], "stats": data["stats"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
