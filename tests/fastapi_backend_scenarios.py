from __future__ import annotations

from pathlib import Path
import sys
import tempfile

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.config import (  # noqa: E402
    AppConfig,
    ChunkingConfig,
    EmbeddingConfig,
    OllamaConfig,
    PathsConfig,
    RetrievalConfig,
)
from mcagent.fastapi_app import create_app  # noqa: E402


def make_temp_config(root: Path) -> AppConfig:
    data = root / "data"
    source = data / "crawler_exports"
    source.mkdir(parents=True, exist_ok=True)
    return AppConfig(
        paths=PathsConfig(
            project_root=root,
            source_dir=source,
            db_path=data / "mcagent.sqlite",
            index_path=data / "vector_index.npz",
        ),
        embedding=EmbeddingConfig(),
        chunking=ChunkingConfig(),
        retrieval=RetrievalConfig(),
        ollama=OllamaConfig(timeout_seconds=1),
    )


def assert_true(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name}: {detail}")


def test_fastapi_core_routes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client = TestClient(create_app(make_temp_config(Path(tmp))))
        health = client.get("/api/health")
        assert_true("health_ok", health.status_code == 200 and health.json().get("backend") == "fastapi")

        agents = client.get("/api/agents")
        assert_true("agents_ok", agents.status_code == 200)
        names = [item.get("id") for item in agents.json().get("agents", [])]
        assert_true("mcagent_present", "mcagent_rag" in names)
        assert_true("crawler_present", "crawler_agent" in names)

        status = client.get("/api/status")
        assert_true("status_ok", status.status_code == 200 and "database" in status.json())

        session = client.post("/api/session", json={"session_id": "fastapi-test"})
        assert_true("session_ok", session.status_code == 200 and session.json().get("history") == [])

        context = client.post("/api/session/context", json={"session_id": "fastapi-test", "agent": "mcagent_rag"})
        context_json = context.json()
        assert_true("context_ok", context.status_code == 200 and context_json.get("session_id") == "fastapi-test")
        assert_true("context_summary", isinstance(context_json.get("summary"), dict))
        assert_true("context_turn_count", context_json.get("turn_count") == 0)

        deleted = client.post("/api/session/delete", json={"session_id": "fastapi-test"})
        assert_true("session_delete_ok", deleted.status_code == 200 and deleted.json().get("session_id") == "fastapi-test")


def test_fastapi_sse_chat_shape() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client = TestClient(create_app(make_temp_config(Path(tmp))))
        with client.stream("POST", "/api/chat/stream", json={"question": "", "session_id": "sse-test"}) as response:
            text = "".join(response.iter_text())
        assert_true("sse_status", response.status_code == 200)
        assert_true("sse_response_event", "event: response" in text)
        assert_true("sse_done_event", "event: done" in text)


def main() -> int:
    test_fastapi_core_routes()
    test_fastapi_sse_chat_shape()
    print("FASTAPI BACKEND SCENARIOS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
