from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .agent_runtime import classify_crawler_tool_result


@dataclass(slots=True)
class CrawlerSelfAuditService:
    """Summarize CrawlerAgent's own evidence review decisions for humans/UI."""

    def build(self, tasks: list[Any], result: dict[str, Any] | None = None) -> dict[str, Any]:
        result = result if isinstance(result, dict) else {}
        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        counts = {"accepted": 0, "rejected": 0, "pending_review": 0, "ingested": 0, "ingest_skipped": 0}

        for item in tasks:
            if not isinstance(item, dict):
                continue
            observation = item.get("observation") if isinstance(item.get("observation"), dict) else classify_crawler_tool_result(item).to_dict()
            status = str(observation.get("status") or "").strip()
            entry = self._task_entry(item, observation)
            if status in {"ok", "duplicate_reused"}:
                accepted.append(entry)
                counts["accepted"] += 1
                if item.get("ingest_deferred"):
                    counts["ingested"] += 1
                if item.get("ingest_skipped"):
                    counts["ingest_skipped"] += 1
            elif status in {"records_pending_review", "uncertain"}:
                pending.append(entry)
                counts["pending_review"] += 1
            else:
                rejected.append(entry)
                counts["rejected"] += 1

        ingest_status = "skipped"
        ingest_note = "No accepted RAG evidence required ingest."
        if result.get("ingest_error"):
            ingest_status = "failed"
            ingest_note = str(result.get("ingest_error") or "")
        elif result.get("ingest"):
            ingest_status = "done"
            ingest_note = "Accepted evidence has been ingested."
        elif result.get("ingest_background"):
            ingest_status = "running"
            ingest_note = "Accepted evidence was handed to background ingest."
        elif counts["ingest_skipped"]:
            ingest_status = "skipped"
            ingest_note = "Accepted evidence was not ingested because the delivery target did not require RAG ingest or it already existed."

        return {
            "counts": counts,
            "ingest_status": ingest_status,
            "ingest_note": ingest_note,
            "review_summary": self._review_summary(counts, ingest_status),
            "accepted_sources": accepted[:12],
            "rejected_sources": rejected[:12],
            "pending_review_sources": pending[:12],
            "principle": "Tools expose objective outputs; CrawlerAgent review decides accept, reject, retry, or ignore for this job.",
        }

    def _task_entry(self, item: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
        validation = item.get("topic_validation") if isinstance(item.get("topic_validation"), dict) else {}
        duplicate_review = item.get("existing_evidence_review") if isinstance(item.get("existing_evidence_review"), dict) else {}
        manifest = item.get("manifest_stats") if isinstance(item.get("manifest_stats"), dict) else {}
        rejected_examples = validation.get("rejected_examples") if isinstance(validation.get("rejected_examples"), list) else []
        accepted_examples = validation.get("examples") if isinstance(validation.get("examples"), list) else []
        objective_evidence = self._objective_evidence(item, observation, manifest)
        accepted_reason = str(validation.get("reason") or item.get("ingest_deferred") or item.get("ingest_skipped") or "")
        rejected_reason = str(validation.get("reason") or duplicate_review.get("reason") or item.get("failure_reason") or observation.get("summary") or "")
        cleanup_action = str(validation.get("cleanup_action") or duplicate_review.get("cleanup_action") or item.get("crawler_review_action") or "")
        next_action = str(validation.get("next_action") or duplicate_review.get("next_action") or item.get("crawler_review_next_action") or observation.get("suggested_next") or "")
        ingest_decision = "deferred" if item.get("ingest_deferred") else ("skipped" if item.get("ingest_skipped") else "")
        return {
            "source": str(item.get("source") or ""),
            "query": str(item.get("query") or ""),
            "status": str(observation.get("status") or ""),
            "summary": str(observation.get("summary") or ""),
            "records": int(manifest.get("records") or 0),
            "usable_records": int(manifest.get("usable_records") or 0),
            "empty_records": int(manifest.get("empty_records") or 0),
            "record_bytes": int(manifest.get("record_bytes") or 0),
            "skipped": int(manifest.get("skipped") or 0),
            "errors": int(manifest.get("errors") or 0),
            "export_dir": str(item.get("export_dir") or ""),
            "accepted_reason": accepted_reason,
            "rejected_reason": rejected_reason,
            "cleanup_action": cleanup_action,
            "next_action": next_action,
            "ingest_decision": ingest_decision,
            "review_note": self._review_note(
                status=str(observation.get("status") or ""),
                accepted_reason=accepted_reason,
                rejected_reason=rejected_reason,
                next_action=next_action,
                ingest_decision=ingest_decision,
            ),
            "objective_evidence": objective_evidence,
            "accepted_examples": accepted_examples[:3],
            "rejected_examples": rejected_examples[:3],
        }

    def _review_summary(self, counts: dict[str, int], ingest_status: str) -> str:
        pieces = [
            f"accepted={counts.get('accepted', 0)}",
            f"rejected={counts.get('rejected', 0)}",
            f"pending_review={counts.get('pending_review', 0)}",
            f"ingest={ingest_status}",
        ]
        return "; ".join(pieces)

    def _review_note(
        self,
        *,
        status: str,
        accepted_reason: str,
        rejected_reason: str,
        next_action: str,
        ingest_decision: str,
    ) -> str:
        if status in {"ok", "duplicate_reused"}:
            reason = accepted_reason or "CrawlerAgent accepted this source for the task."
            ingest = f" Ingest decision: {ingest_decision}." if ingest_decision else ""
            return f"Accepted by CrawlerAgent: {reason}{ingest}"
        if status in {"records_pending_review", "uncertain"}:
            action = next_action or "CrawlerAgent should review, retry, or collect a clearer source before ingest."
            return f"Pending CrawlerAgent review: {action}"
        reason = rejected_reason or "CrawlerAgent did not accept this source for the task."
        action = f" Next action: {next_action}" if next_action else ""
        return f"Rejected by CrawlerAgent: {reason}{action}"

    def _objective_evidence(self, item: dict[str, Any], observation: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
        keys = (
            "url",
            "status_code",
            "content_type",
            "archive_path",
            "download_url",
            "download_status",
            "returncode",
        )
        evidence = {key: item.get(key) for key in keys if item.get(key) not in (None, "")}
        evidence.update(
            {
                "observation_status": str(observation.get("status") or ""),
                "records": int(manifest.get("records") or 0),
                "usable_records": int(manifest.get("usable_records") or 0),
                "record_bytes": int(manifest.get("record_bytes") or 0),
            }
        )
        return evidence
