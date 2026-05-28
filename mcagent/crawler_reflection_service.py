from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from .agent_runtime import classify_crawler_tool_result


@dataclass(slots=True)
class CrawlerReflectionSnapshotService:
    """Build objective loop snapshots for CrawlerAgent reflection."""

    recent_limit: int = 8
    pending_limit: int = 12

    def build(
        self,
        *,
        plan: dict[str, Any],
        task_results: list[dict[str, Any]],
        pending_tasks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        recent_results = [self.compact_result(item) for item in task_results[-self.recent_limit :]]
        pending = [self.compact_pending(index, task) for index, task in enumerate(pending_tasks[: self.pending_limit]) if isinstance(task, dict)]
        statuses: dict[str, int] = {}
        retryable_count = 0
        for item in recent_results:
            status = str(item.get("observation_status") or "unknown")
            statuses[status] = statuses.get(status, 0) + 1
            if item.get("retryable"):
                retryable_count += 1
        return {
            "plan": self.compact_plan(plan),
            "recent_results": recent_results,
            "pending_tasks": pending,
            "observation_statuses": statuses,
            "retryable_recent_results": retryable_count,
            "pressure": self._pressure(statuses=statuses, pending_count=len(pending), recent_count=len(recent_results)),
        }

    def compact_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        return {
            "topic": plan.get("topic"),
            "target_hint": plan.get("target_hint"),
            "delivery_target": plan.get("delivery_target"),
            "requested_by": plan.get("requested_by"),
            "handoff_from": plan.get("handoff_from"),
            "coverage_goals": list(plan.get("coverage_goals") or [])[:8],
            "success_criteria": list(plan.get("success_criteria") or [])[:6],
            "sources": list(plan.get("sources") or [])[:12],
            "artifact_refs": list(plan.get("artifact_refs") or [])[-12:],
        }

    def compact_pending(self, index: int, task: dict[str, Any]) -> dict[str, Any]:
        return {
            "index": index,
            "source": str(task.get("source") or ""),
            "query": str(task.get("query") or ""),
            "reason": str(task.get("reason") or "")[:240],
        }

    def compact_result(self, result: dict[str, Any]) -> dict[str, Any]:
        manifest = result.get("manifest_stats") if isinstance(result.get("manifest_stats"), dict) else {}
        validation = result.get("topic_validation") if isinstance(result.get("topic_validation"), dict) else {}
        reusable = result.get("existing_evidence_reused") if isinstance(result.get("existing_evidence_reused"), dict) else {}
        duplicate_review = result.get("existing_evidence_review") if isinstance(result.get("existing_evidence_review"), dict) else {}
        observation = result.get("observation") if isinstance(result.get("observation"), dict) else classify_crawler_tool_result(result).to_dict()
        return {
            "source": result.get("source"),
            "query": result.get("query"),
            "returncode": result.get("returncode"),
            "observation_status": observation.get("status"),
            "observation_summary": observation.get("summary"),
            "retryable": observation.get("retryable"),
            "suggested_next": observation.get("suggested_next"),
            "records": manifest.get("records"),
            "skipped": manifest.get("skipped"),
            "errors": manifest.get("errors"),
            "matched": validation.get("matched"),
            "validation_reason": validation.get("reason"),
            "crawler_review_action": result.get("crawler_review_action") or validation.get("cleanup_action"),
            "crawler_review_next_action": result.get("crawler_review_next_action") or validation.get("next_action"),
            "rejected_examples": list(validation.get("rejected_examples") or [])[:3],
            "reused_existing": reusable.get("matched"),
            "duplicate_review_reason": duplicate_review.get("reason"),
            "duplicate_review_action": duplicate_review.get("cleanup_action"),
            "duplicate_review_next_action": duplicate_review.get("next_action"),
            "empty": bool(result.get("empty_result")),
            "off_topic": bool(result.get("off_topic_result")),
            "uncertain": bool(result.get("uncertain_result")),
            "records_pending_review": bool(result.get("records_pending_review")),
            "timed_out": bool(result.get("timed_out")),
            "artifact_refs": list(result.get("artifact_refs") or [])[:6],
            "manifest_preview": self._manifest_preview(manifest),
        }

    def _manifest_preview(self, manifest: dict[str, Any]) -> dict[str, Any]:
        manifest_path = Path(str(manifest.get("manifest_path") or ""))
        if not manifest_path.is_file():
            return {}
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            return {}
        return {
            "query": data.get("query") or data.get("search_query") or "",
            "records": self._compact_items(data.get("records"), limit=3),
            "candidates": self._compact_items(data.get("candidates"), limit=5),
            "search_results": self._compact_items(data.get("search_results"), limit=5),
            "skipped": self._compact_items(data.get("skipped"), limit=5),
            "downloads": self._compact_items(data.get("downloads"), limit=3),
            "errors": self._compact_items(data.get("errors"), limit=3),
        }

    def _compact_items(self, value: Any, *, limit: int) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        items: list[dict[str, Any]] = []
        for item in value[:limit]:
            if isinstance(item, dict):
                compact: dict[str, Any] = {}
                for key in (
                    "title",
                    "url",
                    "reason",
                    "status",
                    "status_code",
                    "score",
                    "path",
                    "raw_html_path",
                    "download_url",
                    "filename",
                    "error",
                ):
                    if item.get(key) not in (None, ""):
                        compact[key] = item.get(key)
                if compact:
                    items.append(compact)
            elif str(item).strip():
                items.append({"value": str(item).strip()[:240]})
        return items

    def _pressure(self, *, statuses: dict[str, int], pending_count: int, recent_count: int) -> str:
        if not recent_count:
            return "no_results_yet"
        if statuses.get("quota_limited"):
            return "quota_limited_change_source"
        if statuses.get("login_required") or statuses.get("captcha_required"):
            return "access_blocked_try_public_or_browser_path"
        if statuses.get("empty", 0) + statuses.get("off_topic", 0) >= max(2, recent_count // 2):
            return "poor_yield_replan_queries_or_sources"
        if pending_count == 0:
            return "no_pending_tasks_consider_finish_or_replan"
        if statuses.get("ok") or statuses.get("duplicate_reused"):
            return "has_usable_evidence_check_completion"
        return "continue_with_observation"
