from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".py", ".js", ".css", ".html", ".md", ".json", ".toml", ".txt", ".env"}
SKIP_PARTS = {".git", "data", "runtime", "__pycache__", "node_modules"}

MOJIBAKE_CODES = {
    0x6D94,
    0x58AD,
    0x95AD,
    0x93C1,
    0x9356,
    0x934F,
    0x7D5D,
    0x7ED4,
    0x9416,
    0x8255,
    0x8785,
    0xFFFD,
}

MOJIBAKE_FRAGMENT_CODES = {
    (0x6D63, 0x72B2, 0x30BD),
    (0x93C8, 0xE104, 0x6D00),
    (0x6769, 0x6A3C),
    (0x6D94, 0x583E, 0x7D35),
    (0x9286, 0x3001),
    (0x951B, 0x5C7E),
    (0x9429, 0xE0C6),
    (0x9477, 0xE041),
    (0x935B, 0x5A49),
    (0x7039, 0x7022),
    (0x93B4, 0x621C),
    (0x5BB8, 0x53C9),
    (0x6FBE, 0x6C13),
    (0x741B, 0x6B22),
}
MOJIBAKE_FRAGMENTS = {"".join(chr(code) for code in codes) for codes in MOJIBAKE_FRAGMENT_CODES}

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


def project_text_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_PARTS for part in path.relative_to(ROOT).parts):
            continue
        if path.suffix.lower() in TEXT_SUFFIXES or path.name == ".env":
            files.append(path)
    return files


def test_project_text_files_have_no_bom_or_mojibake() -> None:
    offenders: list[str] = []
    for path in project_text_files():
        raw = path.read_bytes()
        rel = path.relative_to(ROOT).as_posix()
        if raw.startswith(b"\xef\xbb\xbf"):
            offenders.append(f"{rel}: UTF-8 BOM")
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            offenders.append(f"{rel}: invalid UTF-8 at byte {exc.start}")
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            if any(fragment in line for fragment in MOJIBAKE_FRAGMENTS):
                offenders.append(f"{rel}:{line_no}: suspicious mojibake fragment")
                continue
            cluster_hits = sum(1 for char in line if ord(char) in MOJIBAKE_CLUSTER_CODES)
            if cluster_hits >= 3:
                offenders.append(f"{rel}:{line_no}: suspicious mojibake cluster")
                continue
            for char in line:
                code = ord(char)
                if code in MOJIBAKE_CODES:
                    offenders.append(f"{rel}:{line_no}: suspicious mojibake code U+{code:04X}")
                    break
    if offenders:
        raise AssertionError("Encoding hygiene failed:\n" + "\n".join(offenders[:50]))


if __name__ == "__main__":
    test_project_text_files_have_no_bom_or_mojibake()
    print("encoding_hygiene_scenarios passed")
