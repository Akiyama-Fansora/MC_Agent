from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SUPPORTED = {".html", ".htm", ".md", ".markdown", ".json", ".jsonl", ".txt"}
BLOCK_MARKERS = (
    "waf active",
    "503 forbidden",
    "触发防火墙自动拦截",
    "访问被拒绝",
    "web应用防火墙",
)


def read_text(path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def looks_blocked(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in BLOCK_MARKERS)


def classify(path: Path, text: str) -> tuple[str, bool]:
    lowered = f"{path.name}\n{text[:2000]}".lower()
    if looks_blocked(text):
        return "blocked", False
    if "mcmod.cn" in lowered or "mc百科" in lowered or "minecraft" in lowered:
        return "mcmod_candidate", True
    return "other", False


def safe_name(path: Path) -> str:
    digest = hashlib.sha1(str(path).encode("utf-8", errors="replace")).hexdigest()[:8]
    return f"{path.stem}_{digest}{path.suffix.lower()}"


def export_run(run_dir: Path, dest_root: Path) -> Path:
    if not run_dir.exists():
        raise FileNotFoundError(run_dir)
    export_name = f"agenttest_{run_dir.name}"
    export_dir = dest_root / export_name
    raw_dir = export_dir / "raw"
    report_dir = export_dir / "reports"
    raw_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)

    records: list[dict[str, object]] = []
    for path in sorted((run_dir / "outputs").rglob("*")) if (run_dir / "outputs").exists() else []:
        if not path.is_file() or path.suffix.lower() not in SUPPORTED:
            continue
        text = read_text(path)
        status, qa_usable = classify(path, text)
        if status == "other":
            continue
        target = raw_dir / safe_name(path)
        shutil.copy2(path, target)
        records.append(
            {
                "source_path": str(path),
                "export_path": str(target),
                "status": status,
                "qa_usable": qa_usable,
                "size": path.stat().st_size,
            }
        )

    manifest = {
        "manifest_type": "agenttest_run_export",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source_run_dir": str(run_dir),
        "export_dir": str(export_dir),
        "records": records,
        "qa_usable_pages": sum(1 for item in records if item["qa_usable"]),
        "blocked_pages": sum(1 for item in records if item["status"] == "blocked"),
        "import_note": "MCagent ingest skips blocked/WAF pages and imports only usable content.",
    }
    (export_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    report = [
        "# AgentTest Run Export",
        "",
        f"- Source run: `{run_dir}`",
        f"- Records: {len(records)}",
        f"- QA usable pages: {manifest['qa_usable_pages']}",
        f"- Blocked pages: {manifest['blocked_pages']}",
        "",
        "Run `python ingest.py` from `D:\\magic\\MC_Agent` after reviewing the manifest.",
    ]
    (report_dir / "export_report.md").write_text("\n".join(report), encoding="utf-8")
    return export_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="Export MCMod-related artifacts from an AgentTest run.")
    parser.add_argument("run_dir", help="AgentTest run directory, e.g. D:\\magic\\AgentTest\\runs\\...")
    parser.add_argument("--dest", default=str(PROJECT_ROOT / "data" / "crawler_exports"))
    args = parser.parse_args()

    try:
        export_dir = export_run(Path(args.run_dir).resolve(), Path(args.dest).resolve())
    except Exception as exc:  # noqa: BLE001
        print(f"Export failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"Exported to: {export_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
