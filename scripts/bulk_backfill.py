from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import sys
import time
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fetch_mediawiki_seed import DEFAULT_USER_AGENT as WIKI_USER_AGENT
from fetch_mediawiki_seed import fetch_seed as fetch_mediawiki_seed
from fetch_createwiki_seed import DEFAULT_USER_AGENT as CREATEWIKI_USER_AGENT
from fetch_createwiki_seed import fetch_seed as fetch_createwiki_seed
from fetch_ftbwiki_seed import DEFAULT_USER_AGENT as FTBWIKI_USER_AGENT
from fetch_ftbwiki_seed import fetch_seed as fetch_ftbwiki_seed
from fetch_followup_seed import DEFAULT_USER_AGENT as FOLLOWUP_USER_AGENT
from fetch_followup_seed import fetch_followups
from fetch_modrinth_seed import DEFAULT_USER_AGENT as MODRINTH_USER_AGENT
from fetch_modrinth_seed import fetch_seed as fetch_modrinth_seed
from mcagent.config import load_config
from mcagent.ingest import ingest_exports


MEDIAWIKI_TOPICS = [
    "gameplay",
    "survival mode",
    "creative mode",
    "adventure mode",
    "hardcore mode",
    "difficulty",
    "crafting recipe",
    "smelting",
    "enchanting",
    "brewing",
    "trading villager",
    "redstone circuits",
    "command block commands",
    "mob hostile passive neutral",
    "bee honey beehive",
    "farming crops animals",
    "biome",
    "structure village dungeon stronghold",
    "dimension nether end",
    "advancement",
    "multiplayer server",
    "resource pack",
    "data pack",
]


MODRINTH_TOPICS = [
    # User-facing aliases and common MC ecosystem questions.
    "The Bumblezone bee dimension bee world",
    "Twilight Forest bosses",
    "The Aether dimension",
    "Create mod automation",
    "Applied Energistics 2 storage",
    "Mekanism technology",
    "Botania magic",
    "Ars Nouveau magic",
    "Iron's Spells spellbooks",
    "Alex's Mobs animals",
    "Biomes O Plenty worldgen",
    "Oh The Biomes We've Gone",
    "Farmer's Delight food cooking",
    "MineColonies colony",
    "Waystones teleport",
    "JourneyMap map",
    "Xaero map",
    "JEI recipe viewer",
    "EMI recipe viewer",
    "Sodium optimization",
    "Iris shaders",
    "Lithium performance",
    "ModernFix performance",
    "Cobblemon",
    "Pixelmon",
    "Better Combat",
    "RPG adventure bosses",
    "magic adventure dungeon",
    "technology factory automation",
    "storage network",
    "questing modpack",
    "skyblock modpack",
    "fabric utility",
    "forge utility",
    "resource pack vanilla faithful",
    "shader realistic",
]


FTBWIKI_TOPICS = [
    "Twilight Forest bosses",
    "Twilight Forest landmarks",
    "The Aether",
    "Thaumcraft",
    "Tinkers' Construct",
    "IndustrialCraft",
    "BuildCraft",
    "Applied Energistics 2",
    "Botania",
    "Mekanism",
]


CREATEWIKI_TOPICS = [
    "Create mod automation",
    "Create mechanical power",
    "Create trains",
    "Create contraptions",
]


def _slice_topics(topics: list[str], limit: int | None) -> list[str]:
    if limit is None or limit <= 0:
        return topics
    return topics[:limit]


def _write_report(report: dict[str, Any]) -> Path:
    report_dir = PROJECT_ROOT / "data" / "backfill_reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"bulk_backfill_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def run_backfill(args: argparse.Namespace) -> int:
    dest = Path(args.dest).resolve()
    report: dict[str, Any] = {
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "dest": str(dest),
        "profile": args.profile,
        "mediawiki": [],
        "modrinth": [],
        "createwiki": [],
        "ftbwiki": [],
        "followup": None,
        "ingest": None,
        "errors": [],
    }

    if args.profile == "quick":
        wiki_topics = MEDIAWIKI_TOPICS[:8]
        modrinth_topics = MODRINTH_TOPICS[:12]
        createwiki_topics = CREATEWIKI_TOPICS[:2]
        ftbwiki_topics = FTBWIKI_TOPICS[:3]
        wiki_search_limit = min(args.wiki_search_limit, 6)
        mod_limits = {"mod": 8, "modpack": 3, "resourcepack": 2, "shader": 1}
    elif args.profile == "deep":
        wiki_topics = MEDIAWIKI_TOPICS
        modrinth_topics = MODRINTH_TOPICS
        createwiki_topics = CREATEWIKI_TOPICS
        ftbwiki_topics = FTBWIKI_TOPICS
        wiki_search_limit = args.wiki_search_limit
        mod_limits = {"mod": 30, "modpack": 8, "resourcepack": 4, "shader": 2}
    else:
        wiki_topics = MEDIAWIKI_TOPICS
        modrinth_topics = MODRINTH_TOPICS[:24]
        createwiki_topics = CREATEWIKI_TOPICS[:3]
        ftbwiki_topics = FTBWIKI_TOPICS[:6]
        wiki_search_limit = min(args.wiki_search_limit, 10)
        mod_limits = {"mod": 16, "modpack": 5, "resourcepack": 3, "shader": 1}

    wiki_topics = _slice_topics(wiki_topics, args.limit_wiki_topics)
    modrinth_topics = _slice_topics(modrinth_topics, args.limit_modrinth_topics)
    createwiki_topics = _slice_topics(createwiki_topics, args.limit_createwiki_topics)
    ftbwiki_topics = _slice_topics(ftbwiki_topics, args.limit_ftbwiki_topics)

    if not args.modrinth_only:
        print("[1/4] Fetching Minecraft Wiki seed pages...")
        try:
            manifest = fetch_mediawiki_seed(
                dest_root=dest,
                query="",
                search_limit=wiki_search_limit,
                core=True,
                user_agent=WIKI_USER_AGENT,
                force=args.force,
            )
            report["mediawiki"].append(manifest)
            print(f"  core: records={len(manifest['records'])}, skipped={len(manifest['skipped'])}")
        except Exception as exc:  # noqa: BLE001
            report["errors"].append({"source": "mediawiki", "query": "core", "error": str(exc)})
            print(f"  core failed: {exc}")
        for index, query in enumerate(wiki_topics, start=1):
            try:
                manifest = fetch_mediawiki_seed(
                    dest_root=dest,
                    query=query,
                    search_limit=wiki_search_limit,
                    core=False,
                    user_agent=WIKI_USER_AGENT,
                    force=args.force,
                )
                report["mediawiki"].append(manifest)
                print(
                    f"  wiki {index}/{len(wiki_topics)} {query!r}: "
                    f"records={len(manifest['records'])}, skipped={len(manifest['skipped'])}, errors={len(manifest['errors'])}"
                )
            except Exception as exc:  # noqa: BLE001
                report["errors"].append({"source": "mediawiki", "query": query, "error": str(exc)})
                print(f"  wiki {index}/{len(wiki_topics)} {query!r} failed: {exc}")
            time.sleep(args.topic_delay)

    if not args.wiki_only:
        print("[2/4] Fetching Modrinth project pages...")
        for index, query in enumerate(modrinth_topics, start=1):
            try:
                manifest = fetch_modrinth_seed(
                    dest_root=dest,
                    limits=mod_limits,
                    user_agent=MODRINTH_USER_AGENT,
                    delay=args.project_delay,
                    query=query,
                    force=args.force,
                    include_modpack_contents=args.include_modpack_contents,
                    pages=args.modrinth_pages,
                )
                report["modrinth"].append(manifest)
                print(
                    f"  modrinth {index}/{len(modrinth_topics)} {query!r}: "
                    f"records={len(manifest['records'])}, skipped={len(manifest['skipped'])}, errors={len(manifest['errors'])}"
                )
            except Exception as exc:  # noqa: BLE001
                report["errors"].append({"source": "modrinth", "query": query, "error": str(exc)})
                print(f"  modrinth {index}/{len(modrinth_topics)} {query!r} failed: {exc}")
            time.sleep(args.topic_delay)

    if args.createwiki and not args.wiki_only:
        print("[3/6] Fetching Create Wiki pages...")
        for index, query in enumerate(createwiki_topics, start=1):
            try:
                manifest = fetch_createwiki_seed(
                    dest_root=dest,
                    query=query,
                    search_limit=args.createwiki_search_limit,
                    user_agent=CREATEWIKI_USER_AGENT,
                    force=args.force,
                )
                report["createwiki"].append(manifest)
                print(
                    f"  createwiki {index}/{len(createwiki_topics)} {query!r}: "
                    f"records={len(manifest['records'])}, skipped={len(manifest['skipped'])}, errors={len(manifest['errors'])}"
                )
            except Exception as exc:  # noqa: BLE001
                report["errors"].append({"source": "createwiki", "query": query, "error": str(exc)})
                print(f"  createwiki {index}/{len(createwiki_topics)} {query!r} failed: {exc}")
            time.sleep(args.topic_delay)
    else:
        print("[3/6] Skipped Create Wiki.")

    if args.ftbwiki and not args.wiki_only:
        print("[4/6] Fetching FTB Wiki mod pages...")
        for index, query in enumerate(ftbwiki_topics, start=1):
            try:
                manifest = fetch_ftbwiki_seed(
                    dest_root=dest,
                    query=query,
                    search_limit=args.ftbwiki_search_limit,
                    user_agent=FTBWIKI_USER_AGENT,
                    force=args.force,
                )
                report["ftbwiki"].append(manifest)
                print(
                    f"  ftbwiki {index}/{len(ftbwiki_topics)} {query!r}: "
                    f"records={len(manifest['records'])}, skipped={len(manifest['skipped'])}, errors={len(manifest['errors'])}"
                )
            except Exception as exc:  # noqa: BLE001
                report["errors"].append({"source": "ftbwiki", "query": query, "error": str(exc)})
                print(f"  ftbwiki {index}/{len(ftbwiki_topics)} {query!r} failed: {exc}")
            time.sleep(args.topic_delay)
    else:
        print("[4/6] Skipped FTB Wiki.")

    if args.followup and not args.wiki_only:
        print("[5/6] Following public Source/Wiki/README/docs links from Modrinth exports...")
        try:
            manifest = fetch_followups(
                dest_root=dest,
                source_dir=dest,
                user_agent=FOLLOWUP_USER_AGENT,
                max_urls=args.followup_max_urls,
                delay=args.followup_delay,
                include_issues=args.followup_include_issues,
                force=args.force,
            )
            report["followup"] = manifest
            print(
                "  followup: "
                f"records={len(manifest['records'])}, skipped={len(manifest['skipped'])}, "
                f"errors={len(manifest['errors'])}, candidates={manifest['candidates']}"
            )
        except Exception as exc:  # noqa: BLE001
            report["errors"].append({"source": "followup", "query": "modrinth_source_wiki_links", "error": str(exc)})
            print(f"  followup failed: {exc}")
    else:
        print("[5/6] Skipped followup docs.")

    if args.ingest:
        print("[6/6] Ingesting exports into local vector database...")
        config = load_config(args.config)
        stats = ingest_exports(config)
        report["ingest"] = asdict(stats)
        print(
            "  ingest: "
            f"documents_loaded={stats.documents_loaded}, chunks_written={stats.chunks_written}, "
            f"index_vectors={stats.index_vectors}, errors={stats.errors}"
        )
    else:
        print("[6/6] Skipped ingest. Run python ingest.py when ready.")

    report["ended_at"] = datetime.now().isoformat(timespec="seconds")
    report_path = _write_report(report)
    print(f"Report: {report_path}")
    return 0 if not report["errors"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Broadly backfill MCagent's local RAG database from public APIs.")
    parser.add_argument("--dest", default=str(PROJECT_ROOT / "data" / "crawler_exports"))
    parser.add_argument("--config", default=None)
    parser.add_argument("--profile", choices=["quick", "standard", "deep"], default="standard")
    parser.add_argument("--wiki-only", action="store_true")
    parser.add_argument("--modrinth-only", action="store_true")
    parser.add_argument("--ingest", action="store_true", help="Import all crawler exports and rebuild the vector index after fetching.")
    parser.add_argument("--force", action="store_true", help="Write files even when crawl ledger says content is unchanged.")
    parser.add_argument("--limit-wiki-topics", type=int, default=0)
    parser.add_argument("--limit-modrinth-topics", type=int, default=0)
    parser.add_argument("--limit-createwiki-topics", type=int, default=0)
    parser.add_argument("--limit-ftbwiki-topics", type=int, default=0)
    parser.add_argument("--wiki-search-limit", type=int, default=10)
    parser.add_argument("--createwiki", action="store_true", help="Fetch Create mechanics pages from Create Wiki's public MediaWiki API.")
    parser.add_argument("--createwiki-search-limit", type=int, default=12)
    parser.add_argument("--ftbwiki", action="store_true", help="Fetch mod pages from FTB Wiki's public MediaWiki API.")
    parser.add_argument("--ftbwiki-search-limit", type=int, default=10)
    parser.add_argument("--topic-delay", type=float, default=0.15)
    parser.add_argument("--project-delay", type=float, default=0.08)
    parser.add_argument("--modrinth-pages", type=int, default=1, help="Fetch N search result pages for every Modrinth topic and project type.")
    parser.add_argument("--include-modpack-contents", action="store_true", help="Download .mrpack files and extract included mod lists.")
    parser.add_argument("--followup", action="store_true", help="Follow public Source/Wiki/README/docs links found in Modrinth exports.")
    parser.add_argument("--followup-max-urls", type=int, default=80)
    parser.add_argument("--followup-delay", type=float, default=0.12)
    parser.add_argument("--followup-include-issues", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.wiki_only and args.modrinth_only:
        print("--wiki-only and --modrinth-only cannot be used together.", file=sys.stderr)
        return 2
    return run_backfill(args)


if __name__ == "__main__":
    raise SystemExit(main())
