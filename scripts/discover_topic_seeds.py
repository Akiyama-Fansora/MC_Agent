from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import re
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_DIR = PROJECT_ROOT / "data" / "crawler_exports"


STOP_TERMS = {
    "minecraft",
    "mod",
    "mods",
    "query",
    "source",
    "url",
    "markdown",
    "html",
    "manifest",
    "content",
    "metadata",
    "fetch_url",
    "web_discovery",
    "image",
    "from",
    "spm",
    "recommend",
    "more",
    "video",
    "mcmod",
    "lemy",
    "url",
    "bv",
    "search",
    "snippet",
    "fetched",
    "score",
    "落幕曲",
    "整合包",
    "我的世界",
    "视频",
    "教程",
    "攻略",
    "介绍",
    "获取",
    "合成",
    "配方",
    "玩法",
    "资料",
    "百科",
    "最大的",
    "中文",
    "关于百科",
    "关注百科",
    "联系百科",
    "张图片",
    "截图",
    "我的世界实况",
}


def normalize_text(value: str) -> str:
    value = re.sub(r"\[[^\]]+\]\(([^)]+)\)", " ", value)
    value = re.sub(r"https?://\S+", " ", value)
    value = re.sub(r"<!--.*?-->", " ", value, flags=re.S)
    value = re.sub(r"[#*_`>|~-]+", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def target_aliases(query: str) -> list[str]:
    aliases = [query]
    lowered = query.lower()
    if "closing song" in lowered or "落幕曲" in query:
        aliases.extend(["落幕曲", "Closing Song"])
    if "utopia" in lowered or "乌托邦" in query:
        aliases.extend(["乌托邦", "Utopia"])
    return list(dict.fromkeys(alias for alias in aliases if alias.strip()))


def relevant_files(source_dir: Path, aliases: list[str], limit: int) -> list[Path]:
    output: list[tuple[float, Path]] = []
    lowered_aliases = [alias.lower() for alias in aliases]
    for path in source_dir.rglob("*.md"):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        haystack = f"{path}\n{text[:120000]}".lower()
        hits = sum(1 for alias in lowered_aliases if alias.lower() in haystack)
        if hits:
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
            output.append((hits * 10000000000 + mtime, path))
    output.sort(key=lambda item: item[0], reverse=True)
    return [path for _score, path in output[:limit]]


def candidate_lines(path: Path, aliases: list[str]) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines: list[str] = []
    for raw in text.splitlines():
        line = normalize_text(raw)
        if not (4 <= len(line) <= 180):
            continue
        if raw.lstrip().startswith("#") or any(alias.lower() in line.lower() for alias in aliases):
            lines.append(line)
            continue
        if re.search(r"BV[0-9A-Za-z]{8,}|EP\.?\s*\d+|#\s*\d+|Boss|TACZ", line, flags=re.I):
            lines.append(line)
    return lines


def extract_phrases(lines: list[str], aliases: list[str]) -> Counter[str]:
    counter: Counter[str] = Counter()
    alias_pattern = "|".join(re.escape(alias) for alias in aliases if alias)
    for line in lines:
        parts = re.split(r"[，。！？；：:,.!?;、\[\]【】（）()<>《》|/_\-—]+", line)
        for part in parts:
            text = normalize_text(part)
            if not (2 <= len(text) <= 40):
                continue
            text = re.sub(alias_pattern, " ", text, flags=re.I).strip() if alias_pattern else text
            text = normalize_text(text)
            if not (2 <= len(text) <= 24):
                continue
            for phrase in re.findall(r"[A-Za-z][A-Za-z0-9_+-]{2,}|[\u4e00-\u9fff]{2,12}", text):
                if phrase.lower() in STOP_TERMS or phrase in STOP_TERMS:
                    continue
                if re.fullmatch(r"\d+", phrase):
                    continue
                counter[phrase] += 1
        for phrase in re.findall(r"[\u4e00-\u9fff]{2,8}(?:魔法|巫法|金属|方舟|竞技场|聚晶|塔罗牌|牛排|幻化台|法术|饰品|枪械|守护者|亚波伦|六芒星|阎魔刀|灾厄|黑魔法|女仆|聚晶|维度|教堂|边境|武器|防具)", line):
            if phrase not in STOP_TERMS:
                counter[phrase] += 3
    return counter


def build_seed_queries(query: str, phrases: list[str], max_queries: int) -> list[str]:
    base = "落幕曲" if "Closing Song" in query or "落幕曲" in query else query.strip()
    queries: list[str] = []
    for phrase in phrases:
        if phrase.lower() in base.lower():
            continue
        lowered = phrase.lower()
        if lowered in STOP_TERMS or phrase in STOP_TERMS:
            continue
        if re.fullmatch(r"[a-z0-9_+-]{2,}", lowered) and lowered not in {"tacz", "slashblade"}:
            continue
        queries.append(f"{base} {phrase}")
        if len(queries) >= max_queries:
            break
    return queries


def write_report(run_dir: Path, query: str, files: list[Path], phrases: list[str], seed_queries: list[str]) -> Path:
    lines = [
        f"# Topic discovery seeds for {query}",
        "",
        "<!-- source: topic_discovery -->",
        "",
        "## Seed Queries",
        "",
        *[f"- {item}" for item in seed_queries],
        "",
        "## Discovered Phrases",
        "",
        *[f"- {item}" for item in phrases],
        "",
        "## Source Files",
        "",
        *[f"- {path}" for path in files],
        "",
    ]
    path = run_dir / "topic_discovery_report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def discover(source_dir: Path, dest: Path, query: str, max_files: int, max_queries: int) -> dict[str, Any]:
    run_dir = dest / "topic_discovery" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    run_dir.mkdir(parents=True, exist_ok=True)
    aliases = target_aliases(query)
    files = relevant_files(source_dir, aliases, max_files)
    all_lines: list[str] = []
    for path in files:
        all_lines.extend(candidate_lines(path, aliases))
    counter = extract_phrases(all_lines, aliases)
    phrases = [phrase for phrase, _count in counter.most_common(max_queries * 2)]
    seed_queries = build_seed_queries(query, phrases, max_queries)
    report_path = write_report(run_dir, query, files, phrases, seed_queries)
    manifest = {
        "manifest_type": "topic_discovery_export",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "export_dir": str(run_dir),
        "query": query,
        "aliases": aliases,
        "source_files": [str(path) for path in files],
        "discovered_phrases": phrases,
        "seed_queries": seed_queries,
        "records": [
            {
                "title": f"Topic discovery seeds for {query}",
                "url": "",
                "path": str(report_path),
                "chars": report_path.stat().st_size,
            }
        ],
        "skipped": [],
        "errors": [],
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Discover crawler seed queries from existing local topic documents.")
    parser.add_argument("--source-dir", default=str(DEFAULT_SOURCE_DIR))
    parser.add_argument("--dest", default=str(DEFAULT_SOURCE_DIR))
    parser.add_argument("--query", required=True)
    parser.add_argument("--max-files", type=int, default=80)
    parser.add_argument("--max-queries", type=int, default=24)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    manifest = discover(
        source_dir=Path(args.source_dir).resolve(),
        dest=Path(args.dest).resolve(),
        query=args.query.strip(),
        max_files=max(1, min(args.max_files, 500)),
        max_queries=max(1, min(args.max_queries, 100)),
    )
    print(f"Exported to: {manifest['export_dir']}")
    print(f"Seed queries: {len(manifest['seed_queries'])}")
    print(f"Records: {len(manifest['records'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
