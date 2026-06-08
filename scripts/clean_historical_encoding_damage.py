from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import shutil
import sys
from typing import Any

from check_text_encoding import MOJIBAKE_CLUSTER_CODES, MOJIBAKE_TOKENS


ROOT = Path(__file__).resolve().parents[1]
ACTIVE_JSONL_FILES = (
    Path("data/agent_memory.jsonl"),
    Path("data/crawl_ledger.jsonl"),
)
ACTIVE_JSON_FILES = (
    Path("runtime/jobs_history.json"),
)
QUESTION_FIELDS = {"question", "original_user_request", "query", "topic", "target_hint"}
LEDGER_REPAIRABLE_FIELDS = {"query", "title"}
LATIN_MOJIBAKE_MARKERS = ("Ã", "Â", "å", "æ", "ç", "é", "è", "ä", "ð", "ø")
CJK_MOJIBAKE_FRAGMENTS = (
    "氓聨",
    "茅聴",
    "忙聽",
    "莽录",
    "猫碌",
    "盲鹿",
    "脙",
    "脗",
    "锟斤拷",
)


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def suspicious_question_marks(text: str) -> bool:
    compact = re.sub(r"\s+", "", str(text or ""))
    return compact in {"?" * 2, "?" * 3, "?" * 4} or bool(re.fullmatch(r"\?{5,}", compact))


def looks_like_encoding_damage_text(text: str) -> bool:
    value = str(text or "")
    if not value:
        return False
    if "\ufffd" in value:
        return True
    if any(fragment in value for fragment in CJK_MOJIBAKE_FRAGMENTS):
        return True
    if any(token in value for token in MOJIBAKE_TOKENS):
        return True
    cluster_hits = sum(1 for char in value if ord(char) in MOJIBAKE_CLUSTER_CODES)
    if cluster_hits >= 3:
        return True
    c1_controls = sum(1 for char in value if 0x80 <= ord(char) <= 0x9F)
    latin_markers = sum(value.count(marker) for marker in LATIN_MOJIBAKE_MARKERS)
    if c1_controls >= 1 and latin_markers >= 2:
        return True
    return False


def record_damage_reasons(value: Any, path: str = "$") -> list[str]:
    reasons: list[str] = []
    if isinstance(value, str):
        if looks_like_encoding_damage_text(value):
            reasons.append(f"{path}: encoding_damage")
        field = path.rsplit(".", 1)[-1].strip("[]")
        if field in QUESTION_FIELDS and suspicious_question_marks(value):
            reasons.append(f"{path}: unusable_question_marks")
        return reasons
    if isinstance(value, dict):
        for key, item in value.items():
            reasons.extend(record_damage_reasons(item, f"{path}.{key}"))
        return reasons
    if isinstance(value, list):
        for index, item in enumerate(value):
            reasons.extend(record_damage_reasons(item, f"{path}[{index}]"))
        return reasons
    return reasons


def record_repairable_reasons(record: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    for field in LEDGER_REPAIRABLE_FIELDS:
        value = record.get(field)
        if isinstance(value, str) and (looks_like_encoding_damage_text(value) or suspicious_question_marks(value)):
            reasons.append(f"$.{field}: repairable_metadata_damage")
    return reasons


def repair_ledger_record(record: dict[str, Any], reasons: list[str]) -> dict[str, Any]:
    repaired = dict(record)
    repaired_meta = dict(repaired.get("migration_notes") or {})
    repaired_fields: list[str] = list(repaired_meta.get("encoding_cleanup_repaired_fields") or [])
    for field in LEDGER_REPAIRABLE_FIELDS:
        value = repaired.get(field)
        if isinstance(value, str) and (looks_like_encoding_damage_text(value) or suspicious_question_marks(value)):
            repaired[field] = ""
            if field not in repaired_fields:
                repaired_fields.append(field)
    repaired_meta["encoding_cleanup_repaired_fields"] = sorted(repaired_fields)
    repaired_meta["encoding_cleanup_reason"] = "damaged metadata field removed; URL/path/hash retained"
    repaired["migration_notes"] = repaired_meta
    return repaired


def repair_damaged_fields(value: Any) -> tuple[Any, list[str]]:
    reasons: list[str] = []
    if isinstance(value, str):
        if looks_like_encoding_damage_text(value) or suspicious_question_marks(value):
            return "", ["damaged_string_removed"]
        return value, []
    if isinstance(value, dict):
        repaired: dict[str, Any] = {}
        for key, item in value.items():
            clean_item, item_reasons = repair_damaged_fields(item)
            if item_reasons:
                reasons.extend(f"{key}.{reason}" for reason in item_reasons)
            repaired[key] = clean_item
        return repaired, reasons
    if isinstance(value, list):
        repaired_items: list[Any] = []
        for index, item in enumerate(value):
            clean_item, item_reasons = repair_damaged_fields(item)
            if item_reasons:
                reasons.extend(f"[{index}].{reason}" for reason in item_reasons)
            repaired_items.append(clean_item)
        return repaired_items, reasons
    return value, []


def compact_sample(record: Any) -> str:
    text = json.dumps(record, ensure_ascii=False, sort_keys=True)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:500]


def clean_jsonl_file(path: Path, backup_dir: Path, *, apply: bool) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "total_lines": 0,
        "kept_lines": 0,
        "removed_lines": 0,
        "repaired_lines": 0,
        "invalid_json_lines": 0,
        "removed_samples": [],
        "repaired_samples": [],
    }
    if not path.exists():
        return result

    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / path.name
    if apply:
        shutil.copy2(path, backup_path)
    result["backup_path"] = str(backup_path)

    kept: list[str] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_no, raw_line in enumerate(handle, start=1):
            result["total_lines"] += 1
            line = raw_line.rstrip("\n")
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                result["invalid_json_lines"] += 1
                result["removed_lines"] += 1
                if len(result["removed_samples"]) < 20:
                    result["removed_samples"].append(
                        {"line": line_no, "reasons": [f"invalid_json: {exc.msg}"], "sample": line[:500]}
                    )
                continue
            reasons = record_damage_reasons(record)
            if path.name == "crawl_ledger.jsonl" and isinstance(record, dict):
                repairable = record_repairable_reasons(record)
                if repairable and len(repairable) == len(reasons):
                    repaired = repair_ledger_record(record, repairable)
                    kept.append(json.dumps(repaired, ensure_ascii=False, sort_keys=True))
                    result["kept_lines"] += 1
                    result["repaired_lines"] += 1
                    if len(result["repaired_samples"]) < 20:
                        result["repaired_samples"].append(
                            {"line": line_no, "reasons": repairable[:8], "sample": compact_sample(repaired)}
                        )
                    continue
            if reasons:
                result["removed_lines"] += 1
                if len(result["removed_samples"]) < 20:
                    result["removed_samples"].append(
                        {"line": line_no, "reasons": reasons[:8], "sample": compact_sample(record)}
                    )
                continue
            kept.append(json.dumps(record, ensure_ascii=False, sort_keys=True))
            result["kept_lines"] += 1

    if apply:
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        tmp_path.replace(path)
    return result


def clean_jobs_history(path: Path, backup_dir: Path, *, apply: bool) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "total_jobs": 0,
        "kept_jobs": 0,
        "removed_jobs": 0,
        "repaired_jobs": 0,
        "removed_samples": [],
        "repaired_samples": [],
    }
    if not path.exists():
        return result
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / path.name
    if apply:
        shutil.copy2(path, backup_path)
    result["backup_path"] = str(backup_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        result["invalid_json"] = exc.msg
        if apply:
            shutil.move(str(path), str(backup_dir / f"{path.name}.invalid"))
            path.write_text(json.dumps({"version": 1, "jobs_order": [], "jobs": []}, indent=2), encoding="utf-8")
        return result
    if not isinstance(payload, dict):
        result["invalid_shape"] = type(payload).__name__
        return result
    jobs = payload.get("jobs") if isinstance(payload.get("jobs"), list) else []
    result["total_jobs"] = len(jobs)
    kept_jobs: list[dict[str, Any]] = []
    kept_ids: set[str] = set()
    for index, job in enumerate(jobs):
        if not isinstance(job, dict):
            result["removed_jobs"] += 1
            continue
        reasons = record_damage_reasons(job)
        if reasons:
            repaired_job, repair_reasons = repair_damaged_fields(job)
            if isinstance(repaired_job, dict):
                result["repaired_jobs"] += 1
                kept_jobs.append(repaired_job)
                kept_ids.add(str(repaired_job.get("id") or ""))
                if len(result["repaired_samples"]) < 20:
                    result["repaired_samples"].append(
                        {
                            "index": index,
                            "id": job.get("id"),
                            "reasons": reasons[:8],
                            "repair_reasons": repair_reasons[:12],
                            "sample": compact_sample(repaired_job),
                        }
                    )
                continue
            result["removed_jobs"] += 1
            if len(result["removed_samples"]) < 20:
                result["removed_samples"].append(
                    {
                        "index": index,
                        "id": job.get("id"),
                        "reasons": reasons[:8],
                        "sample": compact_sample(job),
                    }
                )
            continue
        kept_jobs.append(job)
        kept_ids.add(str(job.get("id") or ""))
    result["kept_jobs"] = len(kept_jobs)
    if apply and (result["removed_jobs"] or result["repaired_jobs"]):
        new_payload = dict(payload)
        order = payload.get("jobs_order") if isinstance(payload.get("jobs_order"), list) else []
        new_payload["jobs"] = kept_jobs
        new_payload["jobs_order"] = [job_id for job_id in order if str(job_id) in kept_ids]
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(new_payload, ensure_ascii=False, default=str, indent=2), encoding="utf-8")
        tmp_path.replace(path)
    return result


def archive_legacy_cleanup_reports(root: Path, *, apply: bool, stamp: str) -> dict[str, Any]:
    source = root / "data" / "cleanup_reports"
    destination = root / "runtime" / "archives" / f"data_cleanup_reports_{stamp}"
    result = {
        "source": str(source),
        "destination": str(destination),
        "exists": source.exists(),
        "moved": False,
    }
    if not source.exists():
        return result
    if apply:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise FileExistsError(destination)
        shutil.move(str(source), str(destination))
        source.mkdir(parents=True, exist_ok=True)
        (source / "README.md").write_text(
            "# Archived\n\nHistorical cleanup reports were moved to runtime/archives so active data scans do not treat old backups as current knowledge.\n",
            encoding="utf-8",
        )
        result["moved"] = True
    return result


def run_cleanup(root: Path, *, apply: bool) -> dict[str, Any]:
    stamp = now_stamp()
    backup_dir = root / "runtime" / "cleanup_backups" / f"encoding_cleanup_{stamp}"
    report_dir = root / "runtime" / "cleanup_reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    results = {
        "stamp": stamp,
        "apply": apply,
        "jsonl": [
            clean_jsonl_file(root / rel, backup_dir, apply=apply)
            for rel in ACTIVE_JSONL_FILES
        ],
        "json": [
            clean_jobs_history(root / rel, backup_dir, apply=apply)
            for rel in ACTIVE_JSON_FILES
        ],
        "archived_legacy_cleanup_reports": archive_legacy_cleanup_reports(root, apply=apply, stamp=stamp),
    }
    report_path = report_dir / f"encoding_cleanup_{stamp}_{'apply' if apply else 'dry_run'}.json"
    report_path.write_text(json.dumps(results, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    results["report_path"] = str(report_path)
    return results


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Safely remove historical mojibake records from active JSONL data.")
    parser.add_argument("--root", default=str(ROOT), help="Project root. Defaults to this repository.")
    parser.add_argument("--apply", action="store_true", help="Modify files. Without this flag the command only reports.")
    args = parser.parse_args()
    result = run_cleanup(Path(args.root).resolve(), apply=args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
