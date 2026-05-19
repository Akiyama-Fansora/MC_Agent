from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable

from .embeddings import HashingCharNgramEmbedder
from .schema import RawDocument, TextChunk


SCHEMA_VERSION = 1


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_ref TEXT NOT NULL UNIQUE,
            source_path TEXT NOT NULL,
            title TEXT NOT NULL,
            url TEXT,
            content_hash TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            imported_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL,
            chunk_index INTEGER NOT NULL,
            text TEXT NOT NULL,
            start_char INTEGER NOT NULL,
            end_char INTEGER NOT NULL,
            token_estimate INTEGER NOT NULL,
            metadata_json TEXT NOT NULL,
            FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE,
            UNIQUE(document_id, chunk_index)
        );

        CREATE INDEX IF NOT EXISTS idx_chunks_document_id ON chunks(document_id);
        CREATE INDEX IF NOT EXISTS idx_documents_source_path ON documents(source_path);

        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            title,
            source_path,
            text
        );
        """
    )
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
        ("schema_version", str(SCHEMA_VERSION)),
    )
    conn.commit()


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def replace_document(conn: sqlite3.Connection, document: RawDocument, chunks: list[TextChunk]) -> int:
    now = datetime.now(timezone.utc).isoformat()
    doc_hash = content_hash(document.text)
    metadata_json = json.dumps(document.metadata, ensure_ascii=False, sort_keys=True)
    source_path = str(document.source_path)

    conn.execute(
        """
        INSERT INTO documents(source_ref, source_path, title, url, content_hash, metadata_json, imported_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_ref) DO UPDATE SET
            source_path=excluded.source_path,
            title=excluded.title,
            url=excluded.url,
            content_hash=excluded.content_hash,
            metadata_json=excluded.metadata_json,
            imported_at=excluded.imported_at
        """,
        (document.source_ref, source_path, document.title, document.url, doc_hash, metadata_json, now),
    )
    row = conn.execute("SELECT id FROM documents WHERE source_ref = ?", (document.source_ref,)).fetchone()
    if row is None:
        raise RuntimeError(f"Failed to store document: {document.source_ref}")
    document_id = int(row["id"])

    conn.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
    conn.executemany(
        """
        INSERT INTO chunks(document_id, chunk_index, text, start_char, end_char, token_estimate, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                document_id,
                chunk.chunk_index,
                chunk.text,
                chunk.start_char,
                chunk.end_char,
                chunk.token_estimate,
                json.dumps(chunk.metadata, ensure_ascii=False, sort_keys=True),
            )
            for chunk in chunks
        ],
    )
    rows = conn.execute(
        """
        SELECT
            chunks.id AS chunk_id,
            chunks.text AS text
        FROM chunks
        WHERE chunks.document_id = ?
        ORDER BY chunks.chunk_index
        """,
        (document_id,),
    ).fetchall()
    conn.executemany(
        "INSERT OR REPLACE INTO chunks_fts(rowid, title, source_path, text) VALUES (?, ?, ?, ?)",
        [(int(row["chunk_id"]), document.title, source_path, str(row["text"])) for row in rows],
    )
    return document_id


def iter_chunks_for_index(conn: sqlite3.Connection) -> Iterable[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
            chunks.id AS chunk_id,
            chunks.text AS text
        FROM chunks
        ORDER BY chunks.id
        """
    )


def rebuild_vector_index(
    conn: sqlite3.Connection,
    index_path: Path,
    embedder: HashingCharNgramEmbedder,
    batch_size: int = 64,
) -> int:
    np = __import__("numpy")
    index_path.parent.mkdir(parents=True, exist_ok=True)

    chunk_ids: list[int] = []
    matrices: list[Any] = []
    batch_ids: list[int] = []
    batch_texts: list[str] = []

    def flush() -> None:
        nonlocal batch_ids, batch_texts
        if not batch_texts:
            return
        matrices.append(embedder.embed(batch_texts))
        chunk_ids.extend(batch_ids)
        batch_ids = []
        batch_texts = []

    for row in iter_chunks_for_index(conn):
        batch_ids.append(int(row["chunk_id"]))
        batch_texts.append(str(row["text"]))
        if len(batch_texts) >= batch_size:
            flush()
    flush()

    if matrices:
        vectors = np.vstack(matrices).astype(np.float32)
    else:
        vectors = np.zeros((0, embedder.dimension), dtype=np.float32)

    tmp_path = index_path.with_name(index_path.name + ".tmp")
    with tmp_path.open("wb") as fh:
        np.savez_compressed(
            fh,
            chunk_ids=np.asarray(chunk_ids, dtype=np.int64),
            vectors=vectors,
            provider=np.asarray([embedder.provider_name]),
            dimension=np.asarray([embedder.dimension], dtype=np.int64),
            created_at=np.asarray([datetime.now(timezone.utc).isoformat()]),
        )
    tmp_path.replace(index_path)
    return len(chunk_ids)


def fetch_chunks_by_ids(conn: sqlite3.Connection, chunk_ids: list[int]) -> dict[int, sqlite3.Row]:
    if not chunk_ids:
        return {}
    placeholders = ",".join("?" for _ in chunk_ids)
    rows = conn.execute(
        f"""
        SELECT
            chunks.id AS chunk_id,
            chunks.document_id AS document_id,
            chunks.chunk_index AS chunk_index,
            chunks.text AS text,
            chunks.metadata_json AS chunk_metadata_json,
            documents.title AS title,
            documents.source_path AS source_path,
            documents.url AS url,
            documents.metadata_json AS document_metadata_json
        FROM chunks
        JOIN documents ON documents.id = chunks.document_id
        WHERE chunks.id IN ({placeholders})
        """,
        chunk_ids,
    ).fetchall()
    return {int(row["chunk_id"]): row for row in rows}


def rebuild_fts_index(conn: sqlite3.Connection) -> int:
    conn.execute("DROP TABLE IF EXISTS chunks_fts")
    conn.execute(
        """
        CREATE VIRTUAL TABLE chunks_fts USING fts5(
            title,
            source_path,
            text
        )
        """
    )
    rows = conn.execute(
        """
        SELECT
            chunks.id AS chunk_id,
            chunks.text AS text,
            documents.title AS title,
            documents.source_path AS source_path
        FROM chunks
        JOIN documents ON documents.id = chunks.document_id
        ORDER BY chunks.id
        """
    ).fetchall()
    conn.executemany(
        "INSERT INTO chunks_fts(rowid, title, source_path, text) VALUES (?, ?, ?, ?)",
        [
            (int(row["chunk_id"]), str(row["title"]), str(row["source_path"]), str(row["text"]))
            for row in rows
        ],
    )
    return len(rows)


def delete_source_documents_not_in(
    conn: sqlite3.Connection,
    source_dir: Path,
    source_refs: set[str],
) -> int:
    source_root = source_dir.resolve()
    rows = conn.execute("SELECT id, source_ref, source_path FROM documents").fetchall()
    delete_ids: list[int] = []
    for row in rows:
        try:
            source_path = Path(str(row["source_path"])).resolve()
            in_source = source_path.is_relative_to(source_root)
        except OSError:
            in_source = False
        if in_source and str(row["source_ref"]) not in source_refs:
            delete_ids.append(int(row["id"]))
    if not delete_ids:
        return 0
    placeholders = ",".join("?" for _ in delete_ids)
    conn.execute(f"DELETE FROM documents WHERE id IN ({placeholders})", delete_ids)
    return len(delete_ids)


def count_rows(conn: sqlite3.Connection) -> tuple[int, int]:
    doc_count = int(conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
    chunk_count = int(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
    return doc_count, chunk_count
