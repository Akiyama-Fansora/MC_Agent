from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable


@dataclass(frozen=True, slots=True)
class SessionContext:
    session_id: str
    agent: str
    history: list[dict[str, Any]]
    summary: dict[str, Any]
    events: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "agent": self.agent,
            "history": self.history,
            "summary": self.summary,
            "events": self.events,
            "recent_agent_events": self.events[-20:],
            "turn_count": len(self.history),
            "last_turn": self.history[-1] if self.history else None,
        }


class InMemorySessionStore:
    def __init__(self, storage_dir: Path | str | None = None) -> None:
        self._history: dict[str, list[dict[str, Any]]] = {}
        self._summaries: dict[str, dict[str, Any]] = {}
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._loaded: set[str] = set()
        self._storage_dir = Path(storage_dir) if storage_dir is not None else None
        self._lock = threading.Lock()

    def append_turn(self, session_id: str, turn: dict[str, Any], *, max_turns: int = 80) -> None:
        session_id = normalize_session_id(session_id)
        clean_turn = dict(turn)
        clean_turn.setdefault("time", time.time())
        with self._lock:
            self._ensure_loaded_locked(session_id)
            history = self._history.setdefault(session_id, [])
            history.append(clean_turn)
            del history[:-max_turns]
            self._save_locked(session_id)

    def append_event(self, session_id: str, event: dict[str, Any], *, max_events: int = 120) -> None:
        session_id = normalize_session_id(session_id)
        clean_event = dict(event)
        clean_event.setdefault("time", time.time())
        with self._lock:
            self._ensure_loaded_locked(session_id)
            events = self._events.setdefault(session_id, [])
            events.append(clean_event)
            del events[:-max_events]
            self._save_locked(session_id)

    def history(self, session_id: str, *, limit: int | None = None) -> list[dict[str, Any]]:
        session_id = normalize_session_id(session_id)
        with self._lock:
            self._ensure_loaded_locked(session_id)
            history = list(self._history.get(session_id, []))
        if limit is None:
            return history
        return history[-max(1, limit) :]

    def events(self, session_id: str, *, limit: int | None = None) -> list[dict[str, Any]]:
        session_id = normalize_session_id(session_id)
        with self._lock:
            self._ensure_loaded_locked(session_id)
            events = list(self._events.get(session_id, []))
        if limit is None:
            return events
        return events[-max(1, limit) :]

    def summary(self, session_id: str) -> dict[str, Any]:
        session_id = normalize_session_id(session_id)
        with self._lock:
            self._ensure_loaded_locked(session_id)
            return dict(self._summaries.get(session_id) or {})

    def update_summary(self, session_id: str, updater: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
        session_id = normalize_session_id(session_id)
        with self._lock:
            self._ensure_loaded_locked(session_id)
            current = dict(self._summaries.get(session_id) or {})
            updated = updater(current)
            self._summaries[session_id] = dict(updated)
            self._save_locked(session_id)
            return dict(updated)

    def delete(self, session_id: str) -> dict[str, Any]:
        session_id = normalize_session_id(session_id)
        with self._lock:
            self._ensure_loaded_locked(session_id)
            had_history = session_id in self._history
            had_summary = session_id in self._summaries
            had_events = session_id in self._events
            self._history.pop(session_id, None)
            self._summaries.pop(session_id, None)
            self._events.pop(session_id, None)
            self._loaded.discard(session_id)
            path = self._path_for_session(session_id)
            if path and path.exists():
                path.unlink()
        return {"session_id": session_id, "deleted": had_history or had_summary or had_events}

    def context(self, session_id: str, *, agent: str, summary: dict[str, Any] | None = None) -> SessionContext:
        return SessionContext(
            session_id=normalize_session_id(session_id),
            agent=agent or "mcagent_rag",
            history=self.history(session_id),
            summary=dict(summary or self.summary(session_id)),
            events=self.events(session_id),
        )

    def _path_for_session(self, session_id: str) -> Path | None:
        if self._storage_dir is None:
            return None
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", normalize_session_id(session_id))[:120] or "default"
        return self._storage_dir / f"{safe}.json"

    def _ensure_loaded_locked(self, session_id: str) -> None:
        if session_id in self._loaded:
            return
        self._loaded.add(session_id)
        path = self._path_for_session(session_id)
        if path is None or not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        if isinstance(data.get("history"), list):
            self._history[session_id] = [dict(item) for item in data.get("history") if isinstance(item, dict)]
        if isinstance(data.get("summary"), dict):
            self._summaries[session_id] = dict(data.get("summary") or {})
        if isinstance(data.get("events"), list):
            self._events[session_id] = [dict(item) for item in data.get("events") if isinstance(item, dict)]

    def _save_locked(self, session_id: str) -> None:
        path = self._path_for_session(session_id)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "session_id": session_id,
            "updated_at": time.time(),
            "history": self._history.get(session_id, []),
            "summary": self._summaries.get(session_id, {}),
            "events": self._events.get(session_id, []),
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        tmp.replace(path)


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


DEFAULT_SESSION_STORE = InMemorySessionStore(Path(__file__).resolve().parents[1] / "data" / "session_memory")
