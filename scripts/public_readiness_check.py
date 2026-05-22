from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"fc-[A-Za-z0-9_-]{20,}"),
    re.compile(r"tvly-[A-Za-z0-9_-]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
]

REQUIRED_FILES = [
    ".gitattributes",
    ".gitignore",
    ".github/workflows/ci.yml",
    ".env.example",
    "README.md",
    "api.py",
    "config.sample.json",
    "requirements.txt",
    "docs/agent_development_guide.md",
    "frontend/index.html",
    "frontend/settings.html",
    "frontend/static/app.js",
    "frontend/static/app.css",
    "frontend/static/settings.js",
    "mcagent/agent_execution.py",
    "mcagent/agent_executor.py",
    "mcagent/agent_router.py",
    "mcagent/agent_runtime.py",
    "mcagent/crawler_delegation_service.py",
    "mcagent/crawler_reflection_decision_service.py",
    "mcagent/crawler_reflection_service.py",
    "mcagent/crawler_runtime_step_service.py",
    "mcagent/crawler_task_preparation_service.py",
    "mcagent/evidence_service.py",
    "mcagent/event_stream.py",
    "mcagent/fastapi_app.py",
    "mcagent/job_view_service.py",
    "mcagent/llm_profiles.py",
    "mcagent/rag_service.py",
    "mcagent/session_state.py",
    "mcagent/web_server.py",
    "scripts/check_text_encoding.py",
    "tests/crawler_delegation_service_scenarios.py",
    "tests/crawler_reflection_decision_scenarios.py",
    "tests/crawler_reflection_service_scenarios.py",
    "tests/crawler_runtime_step_service_scenarios.py",
    "tests/crawler_task_preparation_service_scenarios.py",
    "tests/agent_execution_scenarios.py",
    "tests/agent_executor_scenarios.py",
    "tests/agent_router_scenarios.py",
    "tests/evidence_service_scenarios.py",
    "tests/job_view_service_scenarios.py",
    "tests/rag_service_scenarios.py",
    "tests/agent_runtime_scenarios.py",
    "tests/backend_services_scenarios.py",
    "tests/fastapi_backend_scenarios.py",
    "tests/smoke_test.py",
]

MAX_TRACKED_FILE_BYTES = 1_000_000


def run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=ROOT, text=True, capture_output=True, check=False)


def git_files() -> list[str]:
    result = run(["git", "ls-files"])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git ls-files failed")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def check_required_files(errors: list[str]) -> None:
    for rel in REQUIRED_FILES:
        if not (ROOT / rel).exists():
            errors.append(f"missing required file: {rel}")


def check_tracked_runtime_data(files: list[str], errors: list[str]) -> None:
    allowed = {"data/.gitkeep", "data/README.md"}
    bad = [path for path in files if path.startswith("data/") and path not in allowed]
    if bad:
        errors.append("runtime data is tracked: " + ", ".join(bad[:20]))


def check_secret_patterns(files: list[str], errors: list[str]) -> None:
    for rel in files:
        path = ROOT / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                errors.append(f"secret-like token found in tracked file: {rel}")
                break


def check_secret_patterns_in_history(errors: list[str]) -> None:
    commits = run(["git", "rev-list", "--all"])
    if commits.returncode != 0:
        errors.append("git history scan failed: " + (commits.stderr.strip() or commits.stdout.strip()))
        return

    pattern = "|".join(pattern.pattern for pattern in SECRET_PATTERNS)
    for commit in [line.strip() for line in commits.stdout.splitlines() if line.strip()]:
        result = run(["git", "grep", "-n", "-I", "-E", pattern, commit, "--"])
        if result.returncode == 0 and result.stdout.strip():
            first = result.stdout.splitlines()[0]
            parts = first.split(":", 2)
            location = ":".join(parts[:2]) if len(parts) >= 2 else commit[:12]
            errors.append(f"secret-like token found in git history near {location}")
            return


def check_tracked_file_sizes(files: list[str], errors: list[str]) -> None:
    large: list[str] = []
    for rel in files:
        path = ROOT / rel
        try:
            if path.is_file() and path.stat().st_size > MAX_TRACKED_FILE_BYTES:
                large.append(f"{rel} ({path.stat().st_size / 1024 / 1024:.1f} MB)")
        except OSError:
            errors.append(f"cannot inspect tracked path: {rel}")
    if large:
        errors.append("large tracked files found: " + ", ".join(large[:20]))


def check_gitignore(errors: list[str]) -> None:
    result = run(
        [
            "git",
            "check-ignore",
            ".env",
            "config.json",
            "data/llm_profiles.json",
            "data/mcagent.sqlite",
            "data/vector_index.npz",
            "data/crawler_exports/example.md",
            "storage_state.json",
            ".auth/session.json",
            "browser_profiles/default/Preferences",
            "pack.zip",
        ]
    )
    ignored = set(result.stdout.splitlines())
    expected = {
        ".env",
        "config.json",
        "data/llm_profiles.json",
        "data/mcagent.sqlite",
        "data/vector_index.npz",
        "data/crawler_exports/example.md",
        "storage_state.json",
        ".auth/session.json",
        "browser_profiles/default/Preferences",
        "pack.zip",
    }
    missing = expected - ignored
    if missing:
        errors.append("gitignore does not ignore: " + ", ".join(sorted(missing)))


def check_public_docs(errors: list[str]) -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    required_phrases = [
        "python ingest.py",
        "python web.py",
        "python api.py",
        "playwright install chromium",
        ".env.example",
        "/settings.html",
        "/docs",
        "本地质量检查",
    ]
    for phrase in required_phrases:
        if phrase not in readme:
            errors.append(f"README missing public setup phrase: {phrase}")
    forbidden_phrases = [
        "GitHub 公开标准",
        "仓库先保持 Private",
        "满足以下条件后再考虑公开",
        "公开前检查",
    ]
    for phrase in forbidden_phrases:
        if phrase in readme:
            errors.append(f"README contains internal publication checklist phrase: {phrase}")


def collect_warnings() -> list[str]:
    warnings: list[str] = []
    if not (ROOT / "LICENSE").exists():
        warnings.append("LICENSE is missing; GitHub can still publish the repository, but reuse rights remain all-rights-reserved until an owner chooses a license.")
    return warnings


def main() -> int:
    errors: list[str] = []
    check_required_files(errors)
    files = git_files()
    check_tracked_runtime_data(files, errors)
    check_secret_patterns(files, errors)
    check_secret_patterns_in_history(errors)
    check_tracked_file_sizes(files, errors)
    check_gitignore(errors)
    check_public_docs(errors)
    warnings = collect_warnings()

    if errors:
        print("PUBLIC READINESS CHECK FAILED")
        for item in errors:
            print(f"- {item}")
        return 1

    print("PUBLIC READINESS CHECK PASSED")
    print(f"tracked_files={len(files)}")
    if warnings:
        print("PUBLIC READINESS WARNINGS")
        for item in warnings:
            print(f"- {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
