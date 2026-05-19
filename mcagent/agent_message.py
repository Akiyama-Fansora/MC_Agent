from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
from typing import Any


@dataclass(slots=True)
class CrawlerTask:
    task_id: str
    question: str
    missing_evidence: str
    topic: str
    preferred_sources: list[str]
    search_queries: list[str]
    output_dir: str
    intent: dict[str, Any] = field(default_factory=dict)
    entity: str = ""
    keywords: list[str] = field(default_factory=list)
    candidate_sources: list[dict[str, Any]] = field(default_factory=list)
    output_format: str = "markdown+manifest"
    success_criteria: list[str] = field(default_factory=list)
    max_urls: int = 40
    priority: int = 50

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "question": self.question,
            "missing_evidence": self.missing_evidence,
            "topic": self.topic,
            "preferred_sources": self.preferred_sources,
            "search_queries": self.search_queries,
            "output_dir": self.output_dir,
            "intent": self.intent,
            "entity": self.entity,
            "keywords": self.keywords,
            "candidate_sources": self.candidate_sources,
            "output_format": self.output_format,
            "success_criteria": self.success_criteria,
            "max_urls": self.max_urls,
            "priority": self.priority,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


@dataclass(slots=True)
class CrawlerResult:
    task_id: str
    status: str
    export_dir: str
    manifest_path: str
    records: int
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    confidence: float = 0.0
    coverage_note: str = ""
    files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "export_dir": self.export_dir,
            "manifest_path": self.manifest_path,
            "records": self.records,
            "skipped": self.skipped,
            "errors": self.errors,
            "confidence": self.confidence,
            "coverage_note": self.coverage_note,
            "files": self.files,
        }


@dataclass(slots=True)
class CollaborationState:
    session_id: str
    question: str
    phase: str = "initial_query"
    round_index: int = 0
    max_rounds: int = 3
    tasks_dispatched: list[CrawlerTask] = field(default_factory=list)
    results_received: list[CrawlerResult] = field(default_factory=list)
    evidence_chain: list[dict[str, Any]] = field(default_factory=list)

    def add_event(self, event_type: str, detail: str | dict[str, Any]) -> None:
        self.evidence_chain.append(
            {
                "time": datetime.now().isoformat(timespec="seconds"),
                "phase": self.phase,
                "event": event_type,
                "detail": detail,
            }
        )

    def can_continue(self) -> bool:
        return self.round_index < self.max_rounds

    def trace_text(self) -> str:
        lines: list[str] = []
        for event in self.evidence_chain:
            detail = event["detail"]
            if isinstance(detail, dict):
                detail = json.dumps(detail, ensure_ascii=False)
            lines.append(f"[{event['phase']}] {event['event']}: {detail}")
        return "\n".join(lines)
