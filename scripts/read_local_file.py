from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = ROOT / "data" / "crawler_exports"


def now_slug() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in value)[:80].strip("-") or "file"


def read_file(path: Path, output_root: Path, *, max_chars: int) -> dict[str, object]:
    run_dir = output_root / "local_file_read" / now_slug()
    run_dir.mkdir(parents=True, exist_ok=True)
    created_at = datetime.now().isoformat(timespec="seconds")
    records: list[dict[str, object]] = []
    errors: list[dict[str, str]] = []
    try:
        resolved = path.expanduser().resolve()
        if not resolved.is_file():
            raise FileNotFoundError(str(resolved))
        raw = resolved.read_text(encoding="utf-8", errors="replace")
        clipped = raw[:max(1, max_chars)]
        digest = hashlib.sha256((str(resolved) + "\n" + clipped).encode("utf-8", errors="ignore")).hexdigest()
        out_path = run_dir / f"{safe_name(resolved.stem)}_{digest[:8]}.md"
        markdown = (
            f"# {resolved.name}\n\n"
            "<!-- source: local_file_read -->\n\n"
            "## Metadata\n\n"
            f"- **Path:** {resolved}\n"
            f"- **Read at:** {created_at}\n"
            f"- **Chars read:** {len(clipped)}\n"
            f"- **Original chars:** {len(raw)}\n\n"
            "## Content\n\n"
            f"{clipped}\n"
        )
        out_path.write_text(markdown, encoding="utf-8")
        records.append({"title": resolved.name, "path": str(out_path), "source_path": str(resolved), "chars": len(clipped), "content_hash": digest})
    except Exception as exc:  # noqa: BLE001
        errors.append({"path": str(path), "error": f"{type(exc).__name__}: {exc}"})

    manifest = {
        "manifest_type": "local_file_read_export",
        "provider": "local_file_read",
        "created_at": created_at,
        "export_dir": str(run_dir),
        "query": str(path),
        "records": records,
        "skipped": [],
        "errors": errors,
        "status": "ok" if records else "failed",
        "failure_reason": "; ".join(item["error"] for item in errors),
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Read one local text file and expose it as a crawler artifact.")
    parser.add_argument("--path", required=True)
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--max-chars", type=int, default=120000)
    args = parser.parse_args()
    manifest = read_file(Path(args.path), Path(args.output_root), max_chars=args.max_chars)
    print(json.dumps({"export_dir": manifest["export_dir"], "records": len(manifest["records"]), "errors": len(manifest["errors"])}, ensure_ascii=False, indent=2))
    print(f"Exported to: {manifest['export_dir']}")
    return 0 if manifest["records"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
