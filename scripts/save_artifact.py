from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.artifact_save_service import ArtifactSaveError, ArtifactSaveService  # noqa: E402


def load_payload(args: argparse.Namespace) -> dict:
    if args.payload:
        return json.loads(Path(args.payload).read_text(encoding="utf-8"))
    payload = {
        "content": args.content or "",
        "format": args.format,
        "path": args.path,
        "filename": args.filename,
        "overwrite": args.overwrite,
    }
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Save a generic agent artifact to a local path.")
    parser.add_argument("--payload", default="", help="JSON payload file containing content, format, path, filename, overwrite, metadata.")
    parser.add_argument("--content", default="")
    parser.add_argument("--format", default="txt")
    parser.add_argument("--path", default="")
    parser.add_argument("--filename", default="")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    try:
        payload = load_payload(args)
        result = ArtifactSaveService().save(
            content=payload.get("content", ""),
            artifact_format=str(payload.get("format") or payload.get("artifact_format") or "txt"),
            path=payload.get("path") or payload.get("output_path") or payload.get("output_dir") or "",
            filename=payload.get("filename") or "",
            overwrite=bool(payload.get("overwrite")),
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
        )
    except (ArtifactSaveError, OSError, TypeError, ValueError) as exc:
        error = {
            "provider": "save_artifact",
            "status": "failed",
            "saved_to_local": False,
            "failure_reason": f"{type(exc).__name__}: {exc}",
        }
        print(json.dumps(error, ensure_ascii=False, indent=2))
        return 1

    print(json.dumps({"provider": "save_artifact", "status": "ok", **result.to_dict()}, ensure_ascii=False, indent=2))
    print(f"Exported to: {result.export_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
