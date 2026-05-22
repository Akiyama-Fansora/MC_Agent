from __future__ import annotations

from typing import Any

from .agent_runtime import classify_crawler_tool_result


FORWARDED_TASK_KEYS = (
    "search_limit",
    "max_urls",
    "mods",
    "modpacks",
    "resourcepacks",
    "shaders",
    "search_depth",
    "max_files",
    "max_queries",
    "max_items",
    "output_dir",
    "start_url",
    "timeout_ms",
    "fields",
    "content",
    "format",
    "artifact_format",
    "path",
    "output_path",
    "filename",
    "overwrite",
    "metadata",
)


class CrawlerTaskPreparationService:
    """Prepare objective tool inputs for a CrawlerAgent-selected task."""

    def build_payload(self, *, base_payload: dict[str, Any], task: dict[str, Any], question: str, task_source: str) -> dict[str, Any]:
        task_query = str(task.get("query") or question).strip()
        task_payload = dict(base_payload)
        task_payload.update({"source": task_source, "query": task_query, "question": question})
        for key in FORWARDED_TASK_KEYS:
            value = task.get(key)
            if value is not None:
                task_payload[key] = value
        return task_payload

    def empty_query_result(self, *, task_source: str, task: dict[str, Any]) -> dict[str, Any]:
        result = {
            "source": task_source,
            "returncode": 2,
            "command": [],
            "output": "Crawler executor refused to run an empty query. This objective failure is returned to CrawlerAgent for reflection/replanning.",
            "timeout_seconds": 0,
            "timed_out": False,
            "export_dir": "",
            "query": "",
            "reason": str(task.get("reason") or ""),
            "manifest_stats": {"records": 0, "skipped": 0, "errors": 0},
            "empty_query_result": True,
            "empty_result": True,
        }
        result["observation"] = classify_crawler_tool_result(result).to_dict()
        return result
