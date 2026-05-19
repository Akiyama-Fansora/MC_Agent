from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
import time


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TARGET_BYTES = 1024 * 1024 * 1024
PROGRESS_PATH = PROJECT_ROOT / "runtime" / "grow_knowledge_base_progress.json"


TOPIC_BATCHES = [
    ["minecraft mod", "minecraft modpack", "minecraft fabric", "minecraft forge", "minecraft neoforge"],
    ["adventure dungeon bosses", "rpg adventure", "magic mod", "technology mod", "automation factory"],
    ["world generation biome", "dimension mod", "structure dungeon", "mobs animals", "bosses"],
    ["performance optimization", "client utility", "server utility", "quality of life", "recipe viewer"],
    ["Create addon", "Twilight Forest", "The Bumblezone", "Aether", "SlashBlade"],
    ["Applied Energistics 2", "Mekanism", "Botania", "Ars Nouveau", "Farmer's Delight"],
    ["Cobblemon", "Pixelmon", "MineColonies", "Waystones", "JourneyMap"],
    ["skyblock modpack", "questing modpack", "kitchen sink modpack", "vanilla plus modpack", "hardcore modpack"],
]


FOCUSED_MODPACK_BATCHES = [
    ["\u843d\u5e55\u66f2 Closing Song \u6574\u5408\u5305", "\u843d\u5e55\u66f2 \u6a21\u7ec4\u5217\u8868", "\u843d\u5e55\u66f2 \u653b\u7565 \u73a9\u6cd5", "\u843d\u5e55\u66f2 \u62d4\u5200\u5251 \u5408\u6210 \u914d\u65b9"],
    ["\u68a6\u60f3\u4e00\u5fc3 \u843d\u5e55\u66f2 \u62d4\u5200\u5251 \u5408\u6210 \u914d\u65b9", "\u5e7b\u9b54 \u96ea\u9e26 \u843d\u5e55\u66f2 \u62d4\u5200\u5251 \u5408\u6210 \u914d\u65b9", "\u51bb\u6a31 \u660e\u517d \u843d\u5e55\u66f2 \u62d4\u5200\u5251 \u5408\u6210 \u914d\u65b9", "\u5929\u5143\u5200 \u5929\u661f\u5200 \u843d\u5e55\u66f2 \u62d4\u5200\u5251 \u5408\u6210 \u914d\u65b9"],
    ["\u4e4c\u6258\u90a6 Utopia \u6574\u5408\u5305", "\u4e4c\u6258\u90a6 \u6a21\u7ec4\u5217\u8868", "Utopia modpack mod list", "Utopia SMP modpack"],
    ["New Utopia modpack", "Utopia femboy Edition modpack", "\u4e4c\u6258\u90a6 \u73a9\u6cd5 \u653b\u7565", "\u4e4c\u6258\u90a6 \u914d\u7f6e \u6a21\u7ec4"],
]



FOCUSED_PROVIDERS = ("mcmod", "modrinth", "tavily", "firecrawl", "jina", "web_discovery")


def log(message: str) -> None:
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {message}", flush=True)


def write_progress(data: dict[str, object]) -> None:
    PROGRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"updated_at": datetime.now().isoformat(timespec="seconds"), **data}
    tmp = PROGRESS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PROGRESS_PATH)


def dir_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                pass
    return total


def run_command(command: list[str], timeout: int) -> dict[str, object]:
    started = time.time()
    log("RUN " + " ".join(str(part) for part in command))
    try:
        completed = subprocess.run(
            command,
            cwd=str(PROJECT_ROOT),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        returncode = completed.returncode
        output = completed.stdout or ""
    except subprocess.TimeoutExpired as exc:
        returncode = 124
        output = (exc.stdout or "") + "\n[TIMEOUT] command exceeded timeout"
    seconds = round(time.time() - started, 2)
    log(f"DONE returncode={returncode} seconds={seconds}")
    return {
        "command": command,
        "returncode": returncode,
        "seconds": seconds,
        "output_tail": output[-4000:],
    }


def write_topic_file(topics: list[str]) -> Path:
    path = PROJECT_ROOT / "runtime" / f"growth_topics_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(topics) + "\n", encoding="utf-8")
    return path


def focused_commands(topic: str, args: argparse.Namespace) -> list[list[str]]:
    return [
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "fetch_mcmod_seed.py"),
            "--query",
            topic,
            "--limit",
            str(args.mcmod_limit),
            "--delay",
            str(args.delay),
        ],
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "fetch_modrinth_seed.py"),
            "--query",
            topic,
            "--mods",
            str(max(8, args.mods // 3)),
            "--modpacks",
            str(args.modpacks),
            "--resourcepacks",
            str(max(4, args.resourcepacks // 3)),
            "--shaders",
            str(max(1, args.shaders // 3)),
            "--pages",
            str(args.pages),
            "--delay",
            str(args.delay),
            "--include-modpack-contents",
        ],
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "fetch_tavily_seed.py"),
            "--query",
            topic,
            "--max-results",
            str(args.web_results),
            "--max-pages",
            str(args.web_pages),
            "--search-depth",
            "advanced",
        ],
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "fetch_firecrawl_seed.py"),
            "--query",
            topic,
            "--max-results",
            str(args.web_results),
            "--max-pages",
            str(args.web_pages),
        ],
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "fetch_jina_seed.py"),
            "--query",
            topic,
            "--max-results",
            str(args.web_results),
            "--max-pages",
            str(args.web_pages),
        ],
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "fetch_web_discovery_seed.py"),
            "--query",
            topic,
            "--max-results",
            str(args.web_results),
            "--max-pages",
            str(args.web_pages),
            "--delay",
            str(args.delay),
        ],
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="Grow MCagent's local public MC knowledge base toward a target size.")
    parser.add_argument("--target-mb", type=int, default=1024)
    parser.add_argument("--cycles", type=int, default=12)
    parser.add_argument("--pages", type=int, default=3)
    parser.add_argument("--mods", type=int, default=100)
    parser.add_argument("--modpacks", type=int, default=40)
    parser.add_argument("--resourcepacks", type=int, default=20)
    parser.add_argument("--shaders", type=int, default=10)
    parser.add_argument("--delay", type=float, default=0.08)
    parser.add_argument("--ingest-every", type=int, default=2)
    parser.add_argument("--command-timeout", type=int, default=900)
    parser.add_argument("--no-ingest", action="store_true")
    parser.add_argument("--topic-profile", choices=["broad", "focused_modpacks"], default="broad")
    parser.add_argument("--mcmod-limit", type=int, default=10)
    parser.add_argument("--web-results", type=int, default=8)
    parser.add_argument("--web-pages", type=int, default=4)
    parser.add_argument("--min-added-mb", type=float, default=1.0, help="Stop after repeated cycles add less than this many MB.")
    parser.add_argument("--low-yield-cycles", type=int, default=2, help="Number of low-yield cycles tolerated before stopping.")
    args = parser.parse_args()

    source_dir = PROJECT_ROOT / "data" / "crawler_exports"
    target_bytes = max(1, args.target_mb) * 1024 * 1024
    report = {
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "target_bytes": target_bytes,
        "cycles": [],
    }
    low_yield_streak = 0
    write_progress(
        {
            "status": "running",
            "topic_profile": args.topic_profile,
            "cycle": 0,
            "cycles_total": max(1, args.cycles),
            "target_bytes": target_bytes,
            "low_yield_streak": low_yield_streak,
            "min_added_bytes": int(max(0.0, args.min_added_mb) * 1024 * 1024),
            "message": "Crawler growth job started.",
        }
    )

    for cycle in range(max(1, args.cycles)):
        before = dir_size(source_dir)
        log(f"cycle={cycle + 1} before_mb={round(before / 1024 / 1024, 2)}")
        if before >= target_bytes:
            break
        topics = (FOCUSED_MODPACK_BATCHES if args.topic_profile == "focused_modpacks" else TOPIC_BATCHES)[cycle % (len(FOCUSED_MODPACK_BATCHES) if args.topic_profile == "focused_modpacks" else len(TOPIC_BATCHES))]
        topic_file = write_topic_file(topics)
        cycle_result = {"cycle": cycle + 1, "before_bytes": before, "topics": topics, "topic_profile": args.topic_profile, "commands": []}
        total_commands = sum(len(focused_commands(topic, args)) if args.topic_profile == "focused_modpacks" else 1 for topic in topics)
        completed_commands = 0
        write_progress(
            {
                "status": "running",
                "topic_profile": args.topic_profile,
                "cycle": cycle + 1,
                "cycles_total": max(1, args.cycles),
                "cycle_before_bytes": before,
                "current_bytes": before,
                "target_bytes": target_bytes,
                "topics": topics,
                "topic_index": 0,
                "commands_completed": completed_commands,
                "commands_total": total_commands,
                "low_yield_streak": low_yield_streak,
                "message": f"Cycle {cycle + 1} started.",
            }
        )
        for topic in topics:
            commands = focused_commands(topic, args) if args.topic_profile == "focused_modpacks" else [[
                    sys.executable,
                    str(PROJECT_ROOT / "scripts" / "fetch_modrinth_seed.py"),
                    "--query",
                    topic,
                    "--mods",
                    str(args.mods),
                    "--modpacks",
                    str(args.modpacks),
                    "--resourcepacks",
                    str(args.resourcepacks),
                    "--shaders",
                    str(args.shaders),
                    "--pages",
                    str(args.pages),
                    "--delay",
                    str(args.delay),
                    "--include-modpack-contents",
                ]]
            for command in commands:
                write_progress(
                    {
                        "status": "running",
                        "topic_profile": args.topic_profile,
                        "cycle": cycle + 1,
                        "cycles_total": max(1, args.cycles),
                        "cycle_before_bytes": before,
                        "current_bytes": dir_size(source_dir),
                        "target_bytes": target_bytes,
                        "topics": topics,
                        "current_topic": topic,
                        "topic_index": topics.index(topic) + 1,
                        "commands_completed": completed_commands,
                        "commands_total": total_commands,
                        "current_command": command,
                        "low_yield_streak": low_yield_streak,
                        "message": "Running crawler provider command.",
                    }
                )
                cycle_result["commands"].append(run_command(command, timeout=max(60, args.command_timeout)))
                completed_commands += 1
                write_progress(
                    {
                        "status": "running",
                        "topic_profile": args.topic_profile,
                        "cycle": cycle + 1,
                        "cycles_total": max(1, args.cycles),
                        "cycle_before_bytes": before,
                        "current_bytes": dir_size(source_dir),
                        "target_bytes": target_bytes,
                        "topics": topics,
                        "current_topic": topic,
                        "topic_index": topics.index(topic) + 1,
                        "commands_completed": completed_commands,
                        "commands_total": total_commands,
                        "current_command": [],
                        "low_yield_streak": low_yield_streak,
                        "message": "Crawler provider command finished.",
                    }
                )
        if not args.no_ingest and (cycle + 1) % max(1, args.ingest_every) == 0:
            write_progress(
                {
                    "status": "ingesting",
                    "topic_profile": args.topic_profile,
                    "cycle": cycle + 1,
                    "cycles_total": max(1, args.cycles),
                    "cycle_before_bytes": before,
                    "current_bytes": dir_size(source_dir),
                    "target_bytes": target_bytes,
                    "commands_completed": completed_commands,
                    "commands_total": total_commands,
                    "low_yield_streak": low_yield_streak,
                    "message": "Importing crawler exports into the local index.",
                }
            )
            cycle_result["ingest"] = run_command([sys.executable, str(PROJECT_ROOT / "ingest.py")], timeout=1800)
        after = dir_size(source_dir)
        cycle_result["after_bytes"] = after
        cycle_result["added_bytes"] = after - before
        min_added_bytes = max(0.0, args.min_added_mb) * 1024 * 1024
        cycle_result["min_added_bytes"] = int(min_added_bytes)
        if after < target_bytes and after - before < min_added_bytes:
            low_yield_streak += 1
        else:
            low_yield_streak = 0
        cycle_result["low_yield_streak"] = low_yield_streak
        log(f"cycle={cycle + 1} after_mb={round(after / 1024 / 1024, 2)} added_mb={round((after - before) / 1024 / 1024, 2)}")
        cycle_result["topic_file"] = str(topic_file)
        report["cycles"].append(cycle_result)
        write_progress(
            {
                "status": "running",
                "topic_profile": args.topic_profile,
                "cycle": cycle + 1,
                "cycles_total": max(1, args.cycles),
                "cycle_before_bytes": before,
                "current_bytes": after,
                "target_bytes": target_bytes,
                "added_bytes": after - before,
                "commands_completed": completed_commands,
                "commands_total": total_commands,
                "low_yield_streak": low_yield_streak,
                "message": f"Cycle {cycle + 1} finished.",
            }
        )
        report_path = PROJECT_ROOT / "data" / "backfill_reports" / "grow_knowledge_base_latest.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        if low_yield_streak >= max(1, args.low_yield_cycles):
            log(
                "Stopping early: "
                f"{low_yield_streak} consecutive low-yield cycles below "
                f"{round(min_added_bytes / 1024 / 1024, 2)} MB."
            )
            report["stopped_reason"] = "low_yield"
            break

    if not args.no_ingest:
        write_progress(
            {
                "status": "ingesting",
                "topic_profile": args.topic_profile,
                "cycle": len(report["cycles"]),
                "cycles_total": max(1, args.cycles),
                "current_bytes": dir_size(source_dir),
                "target_bytes": target_bytes,
                "low_yield_streak": low_yield_streak,
                "message": "Final import is running.",
            }
        )
        report["final_ingest"] = run_command([sys.executable, str(PROJECT_ROOT / "ingest.py")], timeout=1800)
    report["ended_at"] = datetime.now().isoformat(timespec="seconds")
    report["final_bytes"] = dir_size(source_dir)
    out = PROJECT_ROOT / "data" / "backfill_reports" / f"grow_knowledge_base_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_progress(
        {
            "status": "finished",
            "topic_profile": args.topic_profile,
            "cycle": len(report["cycles"]),
            "cycles_total": max(1, args.cycles),
            "current_bytes": report["final_bytes"],
            "target_bytes": target_bytes,
            "low_yield_streak": low_yield_streak,
            "stopped_reason": report.get("stopped_reason", ""),
            "report_path": str(out),
            "message": "Crawler growth job finished.",
        }
    )
    print(f"Report: {out}")
    print(f"Final size: {report['final_bytes']} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
