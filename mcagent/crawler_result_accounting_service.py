from __future__ import annotations

from typing import Any


class CrawlerResultAccountingService:
    """Classify objective crawler tool results into counters and follow-ups."""

    def apply(
        self,
        *,
        result: dict[str, Any],
        task_source: str,
        delivery_target: str,
        followup_query: str,
    ) -> dict[str, Any]:
        manifest = result.get("manifest_stats") if isinstance(result.get("manifest_stats"), dict) else {}
        records_loaded = int(manifest.get("records") or 0)
        usable_records = int(manifest.get("usable_records")) if manifest.get("usable_records") is not None else records_loaded
        empty_records = int(manifest.get("empty_records") or 0)
        returncode = int(result.get("returncode") or 0)
        accounting = {
            "success_delta": 0,
            "candidate_delta": 0,
            "failure_delta": 0,
            "needs_ingest": False,
            "followup_task": None,
        }

        if task_source == "modpack_download" and returncode == 0:
            downloads_loaded = int(manifest.get("downloads") or 0)
            candidates_loaded = int(manifest.get("candidates") or 0)
            blockers_loaded = int(manifest.get("blockers") or 0)
            if downloads_loaded > 0:
                accounting["success_delta"] = 1
                if "rag" in delivery_target.lower() or "mcagent" in delivery_target.lower():
                    accounting["needs_ingest"] = True
                    result["ingest_deferred"] = "CrawlerAgent accepted the downloaded archive evidence; ingest the download evidence and then parse internals."
                else:
                    result["ingest_skipped"] = "CrawlerAgent accepted the downloaded archive evidence for the human-facing task; RAG ingest was not requested."
                result["archive_downloaded"] = True
                accounting["followup_task"] = {
                    "source": "modpack_internal",
                    "query": followup_query,
                    "reason": "Crawler downloaded a public modpack archive; parse internal manifest/modlist/quests/scripts next.",
                    "priority": 146,
                }
            elif candidates_loaded > 0:
                accounting["candidate_delta"] = 1
                result["archive_candidate_found"] = True
                result["archive_not_downloaded"] = True
                result["failure_reason"] = manifest.get("failure_reason") or ""
            else:
                accounting["failure_delta"] = 1
                result["archive_not_found"] = True
                if blockers_loaded > 0:
                    result["archive_access_blocked"] = True
                result["failure_reason"] = manifest.get("failure_reason") or "未发现可公开直接下载的 .mrpack/.zip 整合包包体。"
            return accounting

        if task_source == "mcagent_context" and returncode == 0:
            accounting["candidate_delta"] = 1
            accounting["followup_task"] = {
                "source": "web_discovery",
                "query": followup_query,
                "reason": "MCagent/RAG gap analysis is available; collect public web evidence that fills those gaps instead of treating the diagnostic exchange as final material.",
                "priority": 128,
            }
            result["ingest_skipped"] = "MCagent/RAG context is an inter-agent diagnostic artifact; Crawler uses it for planning but does not re-ingest it as new external evidence."
            return accounting

        if task_source == "fetch_url" and bool(manifest.get("archive_url_detected")):
            accounting["candidate_delta"] = 1
            accounting["followup_task"] = {
                "source": "modpack_download",
                "query": followup_query,
                "reason": "fetch_url reported an exact binary .mrpack/.zip URL; use modpack_download to probe and download the archive objectively.",
                "priority": 148,
            }
            result["archive_url_detected"] = True
            result["ingest_skipped"] = "fetch_url does not extract binary modpack archives; Crawler should use modpack_download for this exact URL."
            return accounting

        if task_source == "topic_discovery" and returncode == 0 and records_loaded > 0:
            accounting["candidate_delta"] = 1
            result["candidate_only"] = True
            result["ingest_skipped"] = "topic_discovery candidates are reviewed by Crawler LLM before follow-up collection"
            return accounting

        if returncode == 0 and bool(result.get("existing_evidence_reused", {}).get("matched")):
            accounting["success_delta"] = 1
            result["ingest_skipped"] = "Crawler reused relevant duplicate-skipped evidence that already exists in the local knowledge base."
            return accounting

        if returncode == 0 and records_loaded > 0 and bool(result.get("topic_validation", {}).get("matched")) and usable_records > 0:
            accounting["success_delta"] = 1
            if "rag" in delivery_target.lower() or "mcagent" in delivery_target.lower():
                accounting["needs_ingest"] = True
                result["ingest_deferred"] = "CrawlerAgent accepted these records; ingest this accepted export after the collection loop finishes."
            else:
                result["ingest_skipped"] = "CrawlerAgent accepted these records for the human-facing task; RAG ingest was not requested."
            return accounting

        if returncode == 0:
            if records_loaded > 0:
                if usable_records <= 0 and empty_records > 0:
                    result["records_pending_review"] = True
                    result["empty_result"] = True
                    result["crawler_review_action"] = "retry_or_rewrite_empty_artifact"
                    result["crawler_review_next_action"] = "CrawlerAgent should reject or rewrite empty saved artifacts before using them as evidence."
                    result["failure_reason"] = "records exist in manifest, but objective file/character counts show no saved content."
                    accounting["failure_delta"] = 1
                    return accounting
                topic_reason = str(result.get("topic_validation", {}).get("reason") or "")
                validation = result.get("topic_validation") if isinstance(result.get("topic_validation"), dict) else {}
                cleanup_action = str(validation.get("cleanup_action") or "").strip()
                if cleanup_action:
                    result["crawler_review_action"] = cleanup_action
                if validation.get("next_action"):
                    result["crawler_review_next_action"] = str(validation.get("next_action") or "")
                if not validation:
                    result["records_pending_review"] = True
                elif topic_reason in {"llm_judge_error_uncertain", "uncertain"}:
                    result["uncertain_result"] = True
                else:
                    result["off_topic_result"] = True
            else:
                result["empty_result"] = True
            accounting["failure_delta"] = 1
            return accounting

        accounting["failure_delta"] = 1
        return accounting
