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
    ".env.example",
    "README.md",
    "config.sample.json",
    "requirements.txt",
    "docs/agent_development_guide.md",
    "frontend/index.html",
    "frontend/static/app.js",
    "frontend/static/app.css",
    "mcagent/web_server.py",
    "scripts/check_text_encoding.py",
    "tests/smoke_test.py",
]


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


def check_gitignore(errors: list[str]) -> None:
    result = run(["git", "check-ignore", ".env", "data/mcagent.sqlite", "data/vector_index.npz", "data/crawler_exports/example.md"])
    ignored = set(result.stdout.splitlines())
    expected = {".env", "data/mcagent.sqlite", "data/vector_index.npz", "data/crawler_exports/example.md"}
    missing = expected - ignored
    if missing:
        errors.append("gitignore does not ignore: " + ", ".join(sorted(missing)))


def check_public_docs(errors: list[str]) -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    required_phrases = [
        "GitHub 公开标准",
        "python ingest.py",
        "python web.py",
        "playwright install chromium",
        ".env.example",
    ]
    for phrase in required_phrases:
        if phrase not in readme:
            errors.append(f"README missing public setup phrase: {phrase}")


def main() -> int:
    errors: list[str] = []
    check_required_files(errors)
    files = git_files()
    check_tracked_runtime_data(files, errors)
    check_secret_patterns(files, errors)
    check_gitignore(errors)
    check_public_docs(errors)

    if errors:
        print("PUBLIC READINESS CHECK FAILED")
        for item in errors:
            print(f"- {item}")
        return 1

    print("PUBLIC READINESS CHECK PASSED")
    print(f"tracked_files={len(files)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
