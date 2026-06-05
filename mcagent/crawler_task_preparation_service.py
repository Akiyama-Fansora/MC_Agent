from __future__ import annotations

import re
from typing import Any

from .agent_runtime import classify_crawler_tool_result
from .crawler_capabilities import task_preflight


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
    "max_chars",
    "max_items",
    "output_dir",
    "start_url",
    "timeout_ms",
    "fields",
    "content",
    "format",
    "artifact_format",
    "path",
    "zip",
    "archive",
    "archive_path",
    "manifest_path",
    "root",
    "file",
    "search_query",
    "pattern",
    "output_path",
    "filename",
    "overwrite",
    "metadata",
    "content_ref",
    "artifact_ref",
    "user_agent",
    "download",
    "no_download",
    "probe_only",
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
        if task_source == "browser_collect":
            self._preserve_browser_collect_constraints(task_payload=task_payload, base_payload=base_payload, task_query=task_query, question=question)
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

    def blocked_preflight_result(self, *, task_source: str, task: dict[str, Any], context_text: str = "") -> dict[str, Any] | None:
        preflight = task_preflight({**task, "source": task_source}, context_text=context_text)
        if preflight.get("valid"):
            return None
        result = {
            "source": task_source,
            "returncode": 2,
            "command": [],
            "output": "Crawler executor refused to run a task that failed objective capability preflight. This contract issue is returned to CrawlerAgent for reflection/replanning.",
            "timeout_seconds": 0,
            "timed_out": False,
            "export_dir": "",
            "query": str(task.get("query") or ""),
            "reason": str(task.get("reason") or ""),
            "manifest_stats": {"records": 0, "skipped": 0, "errors": 0},
            "capability_preflight": preflight,
            "empty_result": True,
        }
        result["observation"] = classify_crawler_tool_result(result).to_dict()
        return result

    def _preserve_browser_collect_constraints(
        self,
        *,
        task_payload: dict[str, Any],
        base_payload: dict[str, Any],
        task_query: str,
        question: str,
    ) -> None:
        combined = "\n".join(
            str(item or "")
            for item in (
                task_query,
                question,
                base_payload.get("original_user_request"),
                base_payload.get("query"),
                base_payload.get("source_question"),
            )
        )
        if not str(task_payload.get("start_url") or "").strip():
            url = self._extract_first_url(combined)
            if url:
                task_payload["start_url"] = url
        if not str(task_payload.get("output_dir") or "").strip():
            path = self._extract_windows_path(combined)
            if path:
                task_payload["output_dir"] = path
        if not task_payload.get("max_items"):
            max_items = self._extract_max_items(combined)
            if max_items:
                task_payload["max_items"] = max_items

    @staticmethod
    def _extract_first_url(text: str) -> str:
        match = re.search(r"https?://[^\s，。；;、)）]+", str(text or ""), flags=re.I)
        return match.group(0).rstrip(".,:;，。；：") if match else ""

    @staticmethod
    def _extract_windows_path(text: str) -> str:
        matches = re.findall(r"[A-Za-z]:\\[^\r\n，。；;]+", str(text or ""))
        if not matches:
            return ""
        value = matches[-1].strip().strip('"').strip("'").rstrip(".,;，。；")
        value = re.sub(r"\s+(?:xlsx|csv|json|md|markdown|report|格式|文件|目录|文件夹|路径|folder|directory).*$", "", value, flags=re.I)
        return value.strip()

    @staticmethod
    def _extract_max_items(text: str) -> int | None:
        value = str(text or "")
        match = re.search(r"(?:前|top\s*)\s*(\d{1,3})\s*(?:个|items?|条|款)?", value, flags=re.I)
        if not match:
            match = re.search(r"(\d{1,3})\s*(?:个|条|款)\s*(?:商品|产品|items?|records?)", value, flags=re.I)
        if not match:
            return None
        return max(1, min(int(match.group(1)), 200))
