from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import csv
import hashlib
import io
import json
from pathlib import Path
import re
from typing import Any

from .config import PROJECT_ROOT


SUPPORTED_ARTIFACT_FORMATS = {"txt", "md", "json", "jsonl", "csv", "html"}


class ArtifactSaveError(ValueError):
    """Raised when an artifact cannot be safely serialized or saved."""


@dataclass(slots=True)
class ArtifactSaveResult:
    ok: bool
    path: str
    format: str
    bytes: int
    sha256: str
    export_dir: str
    manifest_path: str
    created: bool
    overwritten: bool
    saved_to_local: bool = True
    failure_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "path": self.path,
            "format": self.format,
            "bytes": self.bytes,
            "sha256": self.sha256,
            "export_dir": self.export_dir,
            "manifest_path": self.manifest_path,
            "created": self.created,
            "overwritten": self.overwritten,
            "saved_to_local": self.saved_to_local,
            "failure_reason": self.failure_reason,
        }


class ArtifactSaveService:
    """Generic local persistence primitive for agent-created artifacts."""

    def save(
        self,
        *,
        content: Any,
        artifact_format: str,
        path: str | Path | None = None,
        filename: str | None = None,
        overwrite: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactSaveResult:
        normalized_format = self._normalize_format(artifact_format)
        target_path = self._resolve_target_path(path=path, filename=filename, artifact_format=normalized_format)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_existed = target_path.exists()
        final_path = target_path if overwrite else self._unique_path(target_path)
        data = self._serialize(content, normalized_format)
        final_path.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        manifest_path = final_path.parent / "manifest.json"
        manifest = {
            "provider": "save_artifact",
            "status": "ok",
            "saved_to_local": True,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "records": [
                {
                    "path": str(final_path),
                    "format": normalized_format,
                    "bytes": len(data),
                    "sha256": digest,
                    "metadata": metadata or {},
                }
            ],
            "skipped": [],
            "errors": [],
        }
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return ArtifactSaveResult(
            ok=True,
            path=str(final_path),
            format=normalized_format,
            bytes=len(data),
            sha256=digest,
            export_dir=str(final_path.parent),
            manifest_path=str(manifest_path),
            created=not target_existed or final_path != target_path,
            overwritten=overwrite and target_existed and final_path == target_path,
        )

    def _normalize_format(self, artifact_format: str) -> str:
        value = str(artifact_format or "").strip().lower().lstrip(".")
        aliases = {"markdown": "md", "text": "txt", "htm": "html", "ndjson": "jsonl"}
        value = aliases.get(value, value)
        if value not in SUPPORTED_ARTIFACT_FORMATS:
            raise ArtifactSaveError(f"Unsupported artifact format: {artifact_format!r}")
        return value

    def _resolve_target_path(self, *, path: str | Path | None, filename: str | None, artifact_format: str) -> Path:
        base = Path(path).expanduser() if path else PROJECT_ROOT / "data" / "crawler_exports" / "artifacts"
        suffix = f".{artifact_format}"
        if artifact_format == "md":
            suffix = ".md"
        name = self._safe_filename(filename or f"artifact{suffix}")
        if not Path(name).suffix:
            name = f"{name}{suffix}"
        if base.suffix:
            return base.resolve()
        return (base / name).resolve()

    def _safe_filename(self, filename: str) -> str:
        value = str(filename or "").strip()
        value = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value).strip(" ._")
        if not value:
            return "artifact.txt"
        return value[:180]

    def _unique_path(self, path: Path) -> Path:
        if not path.exists():
            return path
        for index in range(2, 1000):
            candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
            if not candidate.exists():
                return candidate
        raise ArtifactSaveError(f"Could not allocate a unique output path near {path}")

    def _serialize(self, content: Any, artifact_format: str) -> bytes:
        if artifact_format == "json":
            if isinstance(content, str):
                try:
                    parsed = json.loads(content)
                except json.JSONDecodeError:
                    parsed = {"content": content}
                text = json.dumps(parsed, ensure_ascii=False, indent=2)
            else:
                text = json.dumps(content, ensure_ascii=False, indent=2)
            return text.encode("utf-8")
        if artifact_format == "jsonl":
            rows = content if isinstance(content, list) else [content]
            return "\n".join(json.dumps(row, ensure_ascii=False) for row in rows).encode("utf-8")
        if artifact_format == "csv":
            return self._serialize_csv(content).encode("utf-8-sig")
        if isinstance(content, str):
            text = content
        else:
            text = json.dumps(content, ensure_ascii=False, indent=2)
        return text.encode("utf-8")

    def _serialize_csv(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        rows = content.get("rows") if isinstance(content, dict) and isinstance(content.get("rows"), list) else content
        output = io.StringIO()
        if isinstance(rows, list) and rows and all(isinstance(row, dict) for row in rows):
            fieldnames: list[str] = []
            for row in rows:
                for key in row.keys():
                    if key not in fieldnames:
                        fieldnames.append(str(key))
            writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
            return output.getvalue()
        writer = csv.writer(output, lineterminator="\n")
        if isinstance(rows, list):
            writer.writerow(["value"])
            for row in rows:
                writer.writerow([json.dumps(row, ensure_ascii=False) if isinstance(row, (dict, list)) else row])
            return output.getvalue()
        if isinstance(rows, dict):
            writer.writerow(["key", "value"])
            for key, value in rows.items():
                writer.writerow([key, json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value])
            return output.getvalue()
        writer.writerow(["value"])
        writer.writerow([rows])
        return output.getvalue()
