from __future__ import annotations

import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

DEFAULT_DIRS = [
    ROOT / "mcagent",
    ROOT / "scripts",
    ROOT / "docs",
    ROOT,
    Path(r"D:\magic\AgentConsole"),
]

TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".json",
    ".ps1",
    ".bat",
    ".js",
    ".css",
    ".html",
    ".txt",
}

EXCLUDE_PARTS = {
    ".git",
    "__pycache__",
    "data",
    "logs",
    "runtime",
    "node_modules",
    ".venv",
    "venv",
}

MOJIBAKE_TOKENS = (
    "\u6d63",
    "\u93c4",
    "\u93c8",
    "\u59ab",
    "\u7ef1",
    "\u74a7",
    "\u93bc",
    "\u9422",
    "\u6d94",
    "\u9286\u3001",
    "\u951b",
    "\u7ecc",
    "\u4e63",
    "\ufffd",
    "\u93b4\u621d\u935d",
    "\u9428\u52ef\u9477",
    "\u93c4\ue21a",
    "\u9359\u53cd\u6226",
    "\u7487\u950b",
    "\u59af\u2033",
    "\u93c9\u30e6",
)


def iter_files(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    output: list[Path] = []
    for base in paths:
        if not base.exists():
            continue
        candidates = [base] if base.is_file() else base.rglob("*")
        for path in candidates:
            if not path.is_file():
                continue
            try:
                rel = path.relative_to(ROOT)
                parts = set(rel.parts)
            except ValueError:
                parts = set(path.parts)
            if parts & EXCLUDE_PARTS:
                continue
            if path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            output.append(path)
    return output


def suspicious_question_run(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if "?" * 3 in stripped or "?" * 4 in stripped:
        # SQL parameter placeholders and short ternary snippets are allowed;
        # long runs of question marks in prose/config are not.
        if "SELECT " in stripped or "VALUES " in stripped or "WHERE " in stripped:
            return False
        if stripped in {"?", "??"}:
            return False
        return True
    return False


def scan_file(path: Path) -> list[tuple[int, str, str]]:
    hits: list[tuple[int, str, str]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        return [(0, "not_utf8", str(exc))]
    for line_no, line in enumerate(text.splitlines(), start=1):
        for token in MOJIBAKE_TOKENS:
            if token in line:
                hits.append((line_no, "mojibake_token", line.strip()))
                break
        if suspicious_question_run(line):
            hits.append((line_no, "question_mark_run", line.strip()))
    return hits


def main() -> int:
    parser = argparse.ArgumentParser(description="Check maintained text files for UTF-8/mojibake regressions.")
    parser.add_argument("paths", nargs="*", help="Optional files or directories to scan.")
    args = parser.parse_args()
    paths = [Path(item) for item in args.paths] if args.paths else DEFAULT_DIRS
    failures: list[tuple[Path, int, str, str]] = []
    for path in iter_files(paths):
        for line_no, kind, snippet in scan_file(path):
            failures.append((path, line_no, kind, snippet))
    if failures:
        for path, line_no, kind, snippet in failures:
            rel = path
            try:
                rel = path.relative_to(ROOT)
            except ValueError:
                pass
            message = f"{rel}:{line_no}: {kind}: {snippet[:180]}"
            print(message.encode("utf-8", errors="backslashreplace").decode("utf-8", errors="replace"))
        return 1
    print("OK: maintained text files are UTF-8 and no mojibake markers were found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
