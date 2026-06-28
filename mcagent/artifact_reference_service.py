from __future__ import annotations

import json
from pathlib import Path
from typing import Any


TEXT_SUFFIXES = {".md", ".txt", ".json", ".jsonl", ".csv", ".html", ".htm", ".xml", ".log"}
PATH_FIELDS = ("path", "markdown_path", "raw_html_path", "raw_text_path", "file", "filepath")


class ArtifactReferenceService:
    """Build and resolve lightweight references to objective tool outputs."""

    def collect_from_result(self, *, result: dict[str, Any], result_index: int, max_refs: int = 12) -> list[dict[str, Any]]:
        manifest = result.get("manifest_stats") if isinstance(result.get("manifest_stats"), dict) else {}
        manifest_path = Path(str(manifest.get("manifest_path") or "")) if manifest.get("manifest_path") else Path("")
        if not manifest_path.is_file():
            export_dir = str(result.get("export_dir") or "").strip()
            manifest_path = Path(export_dir) / "manifest.json" if export_dir else Path("")
        if not manifest_path.is_file():
            return []
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            return []
        records = data.get("records") if isinstance(data.get("records"), list) else []
        refs: list[dict[str, Any]] = []
        for record_index, record in enumerate(records):
            if not isinstance(record, dict):
                continue
            for field in PATH_FIELDS:
                raw_path = str(record.get(field) or "").strip()
                if not raw_path:
                    continue
                path = self._resolve_manifest_record_path(raw_path, manifest_path)
                if path is None:
                    continue
                if not path.exists() or not path.is_file():
                    continue
                ref = {
                    "id": f"r{result_index}.{len(refs) + 1}",
                    "source": result.get("source"),
                    "query": result.get("query"),
                    "title": record.get("title") or path.stem,
                    "url": record.get("url") or "",
                    "path": str(path),
                    "kind": field,
                    "format": path.suffix.lower().lstrip("."),
                    "bytes": self._size(path),
                    "record_index": record_index,
                    "manifest_path": str(manifest_path),
                    "text_like": path.suffix.lower() in TEXT_SUFFIXES,
                }
                refs.append(ref)
                if len(refs) >= max_refs:
                    return refs
        return refs

    def compact_refs(self, refs: list[dict[str, Any]], *, limit: int = 12) -> list[dict[str, Any]]:
        compact: list[dict[str, Any]] = []
        for ref in refs[-limit:]:
            if not isinstance(ref, dict):
                continue
            compact.append(
                {
                    "id": ref.get("id"),
                    "source": ref.get("source"),
                    "title": ref.get("title"),
                    "url": ref.get("url"),
                    "path": ref.get("path"),
                    "kind": ref.get("kind"),
                    "format": ref.get("format"),
                    "bytes": ref.get("bytes"),
                    "text_like": ref.get("text_like"),
                }
            )
        return compact

    def resolve_payload_refs(self, payload: dict[str, Any], refs: list[dict[str, Any]], *, max_chars: int = 200_000) -> dict[str, Any]:
        if payload.get("content") is not None:
            return payload
        ref_id = str(payload.get("content_ref") or payload.get("artifact_ref") or "").strip()
        if not ref_id:
            return payload
        ref = self.resolve_ref(ref_id, refs)
        if not ref:
            payload["content_ref_error"] = f"Artifact reference not found: {ref_id}"
            return payload
        if not bool(ref.get("text_like")):
            payload["content_ref_error"] = f"Artifact reference is not text-like: {ref_id}"
            return payload
        path = Path(str(ref.get("path") or ""))
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            payload["content_ref_error"] = f"{type(exc).__name__}: {exc}"
            return payload
        if len(text) > max_chars:
            text = text[:max_chars] + "\n\n[truncated by ArtifactReferenceService]"
        payload["content"] = text
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        metadata = dict(metadata)
        metadata["resolved_artifact_ref"] = {
            "id": ref.get("id"),
            "path": ref.get("path"),
            "source": ref.get("source"),
            "title": ref.get("title"),
            "url": ref.get("url"),
        }
        payload["metadata"] = metadata
        return payload

    def resolve_ref(self, ref_id: str, refs: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not refs:
            return None
        normalized = ref_id.strip().lower()
        candidates = [ref for ref in refs if isinstance(ref, dict)]
        if normalized in {"latest", "last"}:
            return candidates[-1]
        if normalized.startswith("latest:"):
            suffix = normalized.split(":", 1)[1].lstrip(".")
            for ref in reversed(candidates):
                if str(ref.get("format") or "").lower() == suffix:
                    return ref
            return None
        for ref in candidates:
            if str(ref.get("id") or "").lower() == normalized:
                return ref
        return None

    @staticmethod
    def _size(path: Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return 0

    @staticmethod
    def _resolve_manifest_record_path(raw_path: str, manifest_path: Path) -> Path | None:
        path = Path(raw_path).expanduser()
        if path.is_absolute():
            return path
        base = manifest_path.parent.resolve()
        resolved = (base / path).resolve()
        try:
            resolved.relative_to(base)
        except ValueError:
            return None
        return resolved
