from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Any, Callable


@dataclass(frozen=True, slots=True)
class SessionContext:
    session_id: str
    agent: str
    history: list[dict[str, Any]]
    summary: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "agent": self.agent,
            "history": self.history,
            "summary": self.summary,
            "turn_count": len(self.history),
            "last_turn": self.history[-1] if self.history else None,
        }


class InMemorySessionStore:
    def __init__(self) -> None:
        self._history: dict[str, list[dict[str, Any]]] = {}
        self._summaries: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def append_turn(self, session_id: str, turn: dict[str, Any], *, max_turns: int = 80) -> None:
        session_id = normalize_session_id(session_id)
        clean_turn = dict(turn)
        clean_turn.setdefault("time", time.time())
        with self._lock:
            history = self._history.setdefault(session_id, [])
            history.append(clean_turn)
            del history[:-max_turns]

    def history(self, session_id: str, *, limit: int | None = None) -> list[dict[str, Any]]:
        session_id = normalize_session_id(session_id)
        with self._lock:
            history = list(self._history.get(session_id, []))
        if limit is None:
            return history
        return history[-max(1, limit) :]

    def summary(self, session_id: str) -> dict[str, Any]:
        session_id = normalize_session_id(session_id)
        with self._lock:
            return dict(self._summaries.get(session_id) or {})

    def update_summary(self, session_id: str, updater: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
        session_id = normalize_session_id(session_id)
        with self._lock:
            current = dict(self._summaries.get(session_id) or {})
            updated = updater(current)
            self._summaries[session_id] = dict(updated)
            return dict(updated)

    def delete(self, session_id: str) -> dict[str, Any]:
        session_id = normalize_session_id(session_id)
        with self._lock:
            had_history = session_id in self._history
            had_summary = session_id in self._summaries
            self._history.pop(session_id, None)
            self._summaries.pop(session_id, None)
        return {"session_id": session_id, "deleted": had_history or had_summary}

    def context(self, session_id: str, *, agent: str, summary: dict[str, Any] | None = None) -> SessionContext:
        return SessionContext(
            session_id=normalize_session_id(session_id),
            agent=agent or "mcagent_rag",
            history=self.history(session_id),
            summary=dict(summary or self.summary(session_id)),
        )


def normalize_session_id(value: Any) -> str:
    text = str(value or "default").strip()
    return text or "default"


def payload_history(payload: dict[str, Any], *, limit: int = 10) -> list[dict[str, Any]]:
    raw = payload.get("history")
    if not isinstance(raw, list):
        return []
    turns: list[dict[str, Any]] = []
    pending_question = ""
    pending_time = time.time()
    for item in raw:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "")
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        if role == "user":
            pending_question = text
            try:
                pending_time = float(item.get("time") or time.time() * 1000) / 1000
            except (TypeError, ValueError):
                pending_time = time.time()
            continue
        if role != "assistant" or not pending_question:
            continue
        if text in {"处理中...", "处理 中...", "Processing..."}:
            continue
        turns.append(
            {
                "time": pending_time,
                "question": pending_question,
                "answer": text,
                "sources": item.get("sources") if isinstance(item.get("sources"), list) else [],
            }
        )
        pending_question = ""
        if len(turns) > limit * 2:
            turns = turns[-limit:]
    return turns[-limit:]


def merge_limited(existing: list[Any], new_items: list[Any], *, limit: int) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for item in [*existing, *new_items]:
        value = str(item).strip()
        key = value.lower()
        if not value or key in seen:
            continue
        seen.add(key)
        output.append(value)
        if len(output) >= limit:
            break
    return output


DEFAULT_SESSION_STORE = InMemorySessionStore()
