from __future__ import annotations

from dataclasses import dataclass
import json
import queue
import threading
import traceback
from typing import Any, Callable, Iterator


@dataclass(frozen=True, slots=True)
class StreamEvent:
    event: str
    data: Any

    def to_sse(self) -> str:
        return f"event: {self.event}\ndata: {json.dumps(self.data, ensure_ascii=False, default=str)}\n\n"


class ThreadedEventStream:
    def __init__(self, target: Callable[[Callable[[str, Any], None]], None]) -> None:
        self._target = target
        self._events: queue.Queue[StreamEvent | None] = queue.Queue()

    def emit(self, event: str, data: Any) -> None:
        self._events.put(StreamEvent(event, data))

    def _worker(self) -> None:
        try:
            self._target(self.emit)
        except Exception as exc:  # noqa: BLE001 - stream errors must reach the UI.
            traceback.print_exc()
            error = {"error": f"{type(exc).__name__}: {exc}"}
            self.emit("error", error)
            self.emit(
                "response",
                {
                    "answer": f"Agent 运行时异常：{error['error']}",
                    "sources": [],
                    "context": "",
                    "agent": "runtime",
                    "runtime_error": error,
                },
            )
        finally:
            self._events.put(None)

    def sse(self) -> Iterator[str]:
        threading.Thread(target=self._worker, daemon=True).start()
        while True:
            item = self._events.get()
            if item is None:
                break
            yield item.to_sse()
