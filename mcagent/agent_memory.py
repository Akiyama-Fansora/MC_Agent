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
MOJIBAKE_MARKERS = ("å", "æ", "ç", "é", "è", "ä", "Ã", "Â", "\ufffd")


def append_memory_event(event_type: str, payload: dict[str, Any]) -> None:
    MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "type": event_type,
        **payload,
    }
    with MEMORY_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _looks_like_mojibake_text(value: str) -> bool:
    text = str(value or "")
    if "\ufffd" in text:
        return True
    if any(fragment in text for fragment in ("å", "é", "æ ", "ç¼", "èµ", "ä¹", "Ã", "Â")):
        return True
    if len(text) < 80:
        return False
    marker_count = sum(text.count(marker) for marker in MOJIBAKE_MARKERS)
    cjk_count = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    return marker_count >= 12 and marker_count > cjk_count


def memory_event_has_encoding_damage(event: Any) -> bool:
    if isinstance(event, str):
        return _looks_like_mojibake_text(event)
    if isinstance(event, dict):
        return any(memory_event_has_encoding_damage(item) for item in event.values())
    if isinstance(event, list):
        return any(memory_event_has_encoding_damage(item) for item in event)
    return False


def read_memory_events(limit: int = 40, *, include_damaged: bool = False) -> list[dict[str, Any]]:
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
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not include_damaged and memory_event_has_encoding_damage(event):
            continue
        events.append(event)
    return events


def memory_summary(limit: int = 12) -> dict[str, Any]:
    raw_events = read_memory_events(limit=200, include_damaged=True)
    damaged_count = sum(1 for event in raw_events if memory_event_has_encoding_damage(event))
    events = [event for event in raw_events if not memory_event_has_encoding_damage(event)]
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
        "raw_events_scanned": len(raw_events),
        "encoding_damaged_events_hidden": damaged_count,
        "by_type": by_type,
        "core_memory": core_memory,
        "recall_memory": recent,
    }
