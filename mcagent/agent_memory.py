from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT
from .crawler_planner import CONCEPTS


MEMORY_PATH = PROJECT_ROOT / "data" / "agent_memory.jsonl"
MEMORY_TAIL_BYTES = 2 * 1024 * 1024


def append_memory_event(event_type: str, payload: dict[str, Any]) -> None:
    MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "type": event_type,
        **payload,
    }
    with MEMORY_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def read_memory_events(limit: int = 40) -> list[dict[str, Any]]:
    if not MEMORY_PATH.exists():
        return []
    safe_limit = max(1, int(limit or 1))
    try:
        size = MEMORY_PATH.stat().st_size
        with MEMORY_PATH.open("rb") as handle:
            if size > MEMORY_TAIL_BYTES:
                handle.seek(max(0, size - MEMORY_TAIL_BYTES), os.SEEK_SET)
                handle.readline()
            data = handle.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    events = []
    for line in lines[-safe_limit:]:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def memory_summary(limit: int = 12) -> dict[str, Any]:
    events = read_memory_events(limit=200)
    by_type: dict[str, int] = {}
    recent = events[-limit:]
    for event in events:
        event_type = str(event.get("type") or "unknown")
        by_type[event_type] = by_type.get(event_type, 0) + 1
    core_memory = [
        {
            "canonical": concept["canonical"],
            "primary_source": concept["primary_source"],
            "aliases": concept["aliases"],
        }
        for concept in CONCEPTS
    ]
    return {
        "path": str(MEMORY_PATH),
        "exists": MEMORY_PATH.exists(),
        "events": len(events),
        "by_type": by_type,
        "core_memory": core_memory,
        "recall_memory": recent,
    }
