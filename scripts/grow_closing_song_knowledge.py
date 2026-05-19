from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
import time


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROGRESS_PATH = PROJECT_ROOT / "runtime" / "grow_knowledge_base_progress.json"
SOURCE_DIR = PROJECT_ROOT / "data" / "crawler_exports"
REPORT_DIR = PROJECT_ROOT / "data" / "backfill_reports"


QUERIES = [
    "落幕曲 Closing Song 整合包",
    "落幕曲 MC百科 整合包 教程",
    "落幕曲 新手 攻略 开局",
    "落幕曲 常见问题 喂饭级攻略",
    "落幕曲 FTB 任务系统 攻略",
    "落幕曲 拔刀剑 获取 步骤",
    "落幕曲 拔刀剑 合成 配方",
    "落幕曲 梦想一心 幻魔 雪鸦 冻樱 明兽 天元刀 天星刀 获取",
    "落幕曲 梦想一心 合成 配方 攻略",
    "落幕曲 枪械 tacz 攻略",
    "落幕曲 魔法师 攻略",
    "落幕曲 饰品 塔罗牌 诅咒 攻略",
    "落幕曲 至纯之血 获取",
    "落幕曲 维度撕裂 嬗变台 攻略",
    "落幕曲 毕业装备 推荐",
    "落幕曲 B站 教程",
]


PROVIDERS = ("mcmod", "tavily", "firecrawl", "jina", "web_discovery")


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def dir_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for item in path.rglob("*"):
        if not item.is_file():
            continue
        try:
            total += item.stat().st_size
        except OSError:
            pass
    return total


def write_progress(payload: dict[str, object]) -> None:
    PROGRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = {"updated_at": now(), **payload}
    tmp = PROGRESS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PROGRESS_PATH)


def log(message: str) -> None:
    print(f"[{now()}] {message}", flush=True)


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


def provider_command(provider: str, query: str, args: argparse.Namespace) -> list[str]:
    if provider == "mcmod":
        return [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "fetch_mcmod_seed.py"),
            "--query",
            query,
            "--limit",
            str(args.mcmod_limit),
            "--delay",
            str(args.delay),
        ]
    if provider == "tavily":
        return [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "fetch_tavily_seed.py"),
            "--query",
            query,
            "--max-results",
            str(args.web_results),
            "--max-pages",
            str(args.web_pages),
            "--search-depth",
            "advanced",
        ]
    if provider == "firecrawl":
        return [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "fetch_firecrawl_seed.py"),
            "--query",
            query,
            "--max-results",
            str(args.web_results),
            "--max-pages",
            str(args.web_pages),
        ]
    if provider == "jina":
        return [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "fetch_jina_seed.py"),
            "--query",
            query,
            "--max-results",
            str(args.web_results),
            "--max-pages",
            str(args.web_pages),
        ]
    if provider == "web_discovery":
        return [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "fetch_web_discovery_seed.py"),
            "--query",
            query,
            "--max-results",
            str(args.web_results),
            "--max-pages",
            str(args.web_pages),
        ]
    raise ValueError(f"Unknown provider: {provider}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Focused multi-source backfill for Closing Song / 落幕曲.")
    parser.add_argument("--cycles", type=int, default=2)
    parser.add_argument("--target-mb", type=int, default=384)
    parser.add_argument("--mcmod-limit", type=int, default=10)
    parser.add_argument("--web-results", type=int, default=8)
    parser.add_argument("--web-pages", type=int, default=5)
    parser.add_argument("--delay", type=float, default=0.08)
    parser.add_argument("--command-timeout", type=int, default=900)
    parser.add_argument("--ingest-every", type=int, default=1)
    parser.add_argument("--skip-cleanup", action="store_true")
    args = parser.parse_args()

    target_bytes = max(1, args.target_mb) * 1024 * 1024
    total_commands = max(1, args.cycles) * len(QUERIES) * len(PROVIDERS)
    completed = 0
    report = {
        "started_at": now(),
        "topic_profile": "closing_song_focused",
        "queries": QUERIES,
        "providers": PROVIDERS,
        "cycles": [],
    }
    write_progress(
        {
            "status": "running",
            "topic_profile": "closing_song_focused",
            "cycle": 0,
            "cycles_total": max(1, args.cycles),
            "commands_completed": 0,
            "commands_total": total_commands,
            "current_bytes": dir_size(SOURCE_DIR),
            "target_bytes": target_bytes,
            "message": "Closing Song focused crawler started.",
        }
    )

    for cycle in range(1, max(1, args.cycles) + 1):
        before = dir_size(SOURCE_DIR)
        cycle_report = {"cycle": cycle, "before_bytes": before, "commands": []}
        for query in QUERIES:
            for provider in PROVIDERS:
                command = provider_command(provider, query, args)
                write_progress(
                    {
                        "status": "running",
                        "topic_profile": "closing_song_focused",
                        "cycle": cycle,
                        "cycles_total": max(1, args.cycles),
                        "current_topic": query,
                        "current_provider": provider,
                        "current_command": command,
                        "commands_completed": completed,
                        "commands_total": total_commands,
                        "current_bytes": dir_size(SOURCE_DIR),
                        "target_bytes": target_bytes,
                        "message": "Running Closing Song provider command.",
                    }
                )
                result = run_command(command, timeout=max(60, args.command_timeout))
                result["provider"] = provider
                result["query"] = query
                cycle_report["commands"].append(result)
                completed += 1
                write_progress(
                    {
                        "status": "running",
                        "topic_profile": "closing_song_focused",
                        "cycle": cycle,
                        "cycles_total": max(1, args.cycles),
                        "current_topic": query,
                        "current_provider": provider,
                        "current_command": [],
                        "commands_completed": completed,
                        "commands_total": total_commands,
                        "current_bytes": dir_size(SOURCE_DIR),
                        "target_bytes": target_bytes,
                        "message": "Closing Song provider command finished.",
                    }
                )
        if cycle % max(1, args.ingest_every) == 0:
            write_progress(
                {
                    "status": "ingesting",
                    "topic_profile": "closing_song_focused",
                    "cycle": cycle,
                    "cycles_total": max(1, args.cycles),
                    "commands_completed": completed,
                    "commands_total": total_commands,
                    "current_bytes": dir_size(SOURCE_DIR),
                    "target_bytes": target_bytes,
                    "message": "Importing Closing Song exports.",
                }
            )
            cycle_report["ingest"] = run_command([sys.executable, str(PROJECT_ROOT / "ingest.py")], timeout=1800)
        cycle_report["after_bytes"] = dir_size(SOURCE_DIR)
        cycle_report["added_bytes"] = cycle_report["after_bytes"] - before
        report["cycles"].append(cycle_report)
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        (REPORT_DIR / "grow_closing_song_latest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        if cycle_report["after_bytes"] >= target_bytes:
            report["stopped_reason"] = "target_reached"
            break

    if not args.skip_cleanup:
        write_progress(
            {
                "status": "cleanup",
                "topic_profile": "closing_song_focused",
                "cycle": len(report["cycles"]),
                "cycles_total": max(1, args.cycles),
                "commands_completed": completed,
                "commands_total": total_commands,
                "current_bytes": dir_size(SOURCE_DIR),
                "target_bytes": target_bytes,
                "message": "Cleaning duplicate exports by normalized URL.",
            }
        )
        report["cleanup"] = run_command(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "cleanup_duplicate_exports.py"), "--apply"],
            timeout=600,
        )

    write_progress(
        {
            "status": "ingesting",
            "topic_profile": "closing_song_focused",
            "cycle": len(report["cycles"]),
            "cycles_total": max(1, args.cycles),
            "commands_completed": completed,
            "commands_total": total_commands,
            "current_bytes": dir_size(SOURCE_DIR),
            "target_bytes": target_bytes,
            "message": "Final Closing Song import is running.",
        }
    )
    report["final_ingest"] = run_command([sys.executable, str(PROJECT_ROOT / "ingest.py")], timeout=1800)
    report["ended_at"] = now()
    report["final_bytes"] = dir_size(SOURCE_DIR)
    out = REPORT_DIR / f"grow_closing_song_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_progress(
        {
            "status": "finished",
            "topic_profile": "closing_song_focused",
            "cycle": len(report["cycles"]),
            "cycles_total": max(1, args.cycles),
            "commands_completed": completed,
            "commands_total": total_commands,
            "current_bytes": report["final_bytes"],
            "target_bytes": target_bytes,
            "report_path": str(out),
            "message": "Closing Song focused crawler finished.",
        }
    )
    print(f"Report: {out}")
    print(f"Final size: {report['final_bytes']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
