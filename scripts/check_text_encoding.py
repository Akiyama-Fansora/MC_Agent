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
    "\u93b7\u57ae",
    "\u7a0c\u7a7a",
    "\u74ba\u621d",
    "\u95c7\u20ac",
    "\u7470\u52e7",
    "\u6769\u68a9",
    "\u95b2\u56ec",
    "\u5bb8\u63d2",
    "\u6fbe\u8fab",
    "\u9359\u53cd",
    "\u9429\u52ef",
    "\u59dd\u30e6",
    "\u7eef\u31bd",
    "\u7ef1\u30e4",
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
    "\u93c1\u6751\u608e\u9356",
)

MOJIBAKE_CLUSTER_CODES = {
    0x6D63,
    0x72B2,
    0x30BD,
    0x93C8,
    0xE104,
    0x6D00,
    0x6769,
    0x6A3C,
    0x583E,
    0x951B,
    0x9286,
    0x7487,
    0x9477,
    0x9429,
    0x935B,
    0x7039,
    0x93B4,
    0x93C4,
    0x95B2,
    0x6FBE,
    0x7C2E,
    0x741B,
    0x7C31,
    0x5BB8,
    0x60CE,
    0x93B5,
    0x7571,
    0x7D35,
    0x4FD3,
}


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
        if "encoding-check: allow" in line:
            continue
        for token in MOJIBAKE_TOKENS:
            if token in line:
                hits.append((line_no, "mojibake_token", line.strip()))
                break
        cluster_hits = sum(1 for char in line if ord(char) in MOJIBAKE_CLUSTER_CODES)
        if cluster_hits >= 3:
            hits.append((line_no, "mojibake_cluster", line.strip()))
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
