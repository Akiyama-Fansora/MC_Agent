from __future__ import annotations

from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcagent.agent_execution import AgentTraceRecorder, build_agent_execution_context, resolve_agent_model  # noqa: E402
from mcagent.config import AppConfig, ChunkingConfig, EmbeddingConfig, OllamaConfig, PathsConfig, RetrievalConfig  # noqa: E402


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
        ollama=OllamaConfig(model="unit-test-model", temperature=0.25),
    )


def assert_equal(name: str, actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"{name}: expected {expected!r}, got {actual!r}")


def assert_true(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name}: {detail}")


def test_trace_recorder_keeps_legacy_shape_and_emits() -> None:
    emitted: list[tuple[str, object]] = []
    recorder = AgentTraceRecorder(emit=lambda event, data: emitted.append((event, data)))
    step = recorder.add("observe", "received", {"question": "你好"})
    response = recorder.response({"answer": "ok"})
    recorder.delta("chunk")

    assert_equal("step_stage", step["stage"], "observe")
    assert_equal("step_status", step["status"], "received")
    assert_true("step_time", isinstance(step["time"], float))
    assert_equal("response_trace", response["trace"], recorder.steps)
    assert_equal("emitted_trace_event", emitted[0][0], "trace")
    assert_equal("emitted_delta_event", emitted[-1][0], "delta")


def test_execution_context_resolves_request_and_model() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        config = make_temp_config(Path(tmp))
        payload = {"question": "你好", "agent": "mcagent_rag", "model": "explicit-model", "temperature": 0.4}
        context = build_agent_execution_context(config, payload, token_resolver=lambda _payload, question: len(question))

    assert_equal("original_question", context.original_question, "你好")
    assert_equal("question", context.question, "你好")
    assert_equal("agent", context.agent, "mcagent_rag")
    assert_equal("model", context.model, "explicit-model")
    assert_equal("temperature", context.temperature, 0.4)
    assert_equal("max_tokens", context.max_tokens, 2)
    assert_equal("observe_trace", context.trace.steps[0]["stage"], "observe")


def test_model_resolution_precedence() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        config = make_temp_config(Path(tmp))
        assert_equal("profile_override", resolve_agent_model(config, {"model_profile_id": "abc"}, "mcagent_rag"), "profile:abc")
        assert_equal("raw_model_override", resolve_agent_model(config, {"model": "raw"}, "mcagent_rag"), "raw")
        assert_true("auto_uses_assignment_profile", resolve_agent_model(config, {"model": "auto"}, "mcagent_rag").startswith("profile:"))
        assert_true("default_uses_assignment_profile", resolve_agent_model(config, {"model": "default"}, "crawler_agent").startswith("profile:"))
        assert_true("default_assignment_profile", resolve_agent_model(config, {}, "mcagent_rag").startswith("profile:"))


def main() -> int:
    test_trace_recorder_keeps_legacy_shape_and_emits()
    test_execution_context_resolves_request_and_model()
    test_model_resolution_precedence()
    print("AGENT EXECUTION SCENARIOS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
