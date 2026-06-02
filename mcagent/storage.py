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

    old_chunk_ids = [
        int(old_row["id"])
        for old_row in conn.execute("SELECT id FROM chunks WHERE document_id = ?", (document_id,)).fetchall()
    ]
    if old_chunk_ids:
        placeholders = ",".join("?" for _ in old_chunk_ids)
        conn.execute(f"DELETE FROM chunks_fts WHERE rowid IN ({placeholders})", old_chunk_ids)
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


def iter_chunks_by_ids_for_index(conn: sqlite3.Connection, chunk_ids: list[int]) -> Iterable[sqlite3.Row]:
    if not chunk_ids:
        return []
    rows: list[sqlite3.Row] = []
    for offset in range(0, len(chunk_ids), 900):
        batch = chunk_ids[offset : offset + 900]
        placeholders = ",".join("?" for _ in batch)
        rows.extend(
            conn.execute(
                f"""
                SELECT
                    chunks.id AS chunk_id,
                    chunks.text AS text
                FROM chunks
                WHERE chunks.id IN ({placeholders})
                ORDER BY chunks.id
                """,
                batch,
            ).fetchall()
        )
    return rows


def chunk_ids_for_source_roots(conn: sqlite3.Connection, source_roots: list[Path]) -> list[int]:
    resolved_roots = [root.resolve() for root in source_roots]
    if not resolved_roots:
        return []
    document_ids: list[int] = []
    for row in conn.execute("SELECT id, source_path FROM documents").fetchall():
        try:
            source_path = Path(str(row["source_path"])).resolve()
        except OSError:
            continue
        if any(source_path.is_relative_to(root) for root in resolved_roots):
            document_ids.append(int(row["id"]))
    if not document_ids:
        return []
    chunk_ids: list[int] = []
    for offset in range(0, len(document_ids), 900):
        batch = document_ids[offset : offset + 900]
        placeholders = ",".join("?" for _ in batch)
        chunk_ids.extend(
            int(row["id"])
            for row in conn.execute(
                f"SELECT id FROM chunks WHERE document_id IN ({placeholders}) ORDER BY id",
                batch,
            ).fetchall()
        )
    return sorted(set(chunk_ids))


def _existing_chunk_ids(conn: sqlite3.Connection, chunk_ids: list[int]) -> set[int]:
    existing: set[int] = set()
    for offset in range(0, len(chunk_ids), 900):
        batch = chunk_ids[offset : offset + 900]
        if not batch:
            continue
        placeholders = ",".join("?" for _ in batch)
        existing.update(
            int(row["id"])
            for row in conn.execute(
                f"SELECT id FROM chunks WHERE id IN ({placeholders})",
                batch,
            ).fetchall()
        )
    return existing


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
    delta_path = index_path.with_name(f"{index_path.stem}.delta{index_path.suffix}")
    if delta_path.exists():
        delta_path.unlink()
    return len(chunk_ids)


def update_vector_index_for_chunks(
    conn: sqlite3.Connection,
    index_path: Path,
    embedder: HashingCharNgramEmbedder,
    chunk_ids: list[int],
    batch_size: int = 64,
    max_new_chunks: int | None = 256,
) -> int:
    np = __import__("numpy")
    delta_path = index_path.with_name(f"{index_path.stem}.delta{index_path.suffix}")
    delta_path.parent.mkdir(parents=True, exist_ok=True)
    target_chunk_ids = sorted(set(chunk_ids))
    target_chunk_id_set = set(target_chunk_ids)
    main_indexed_ids: set[int] = set()
    if index_path.exists():
        main_index = None
        try:
            main_index = np.load(index_path, allow_pickle=False)
            main_indexed_ids = {int(value) for value in main_index["chunk_ids"].tolist()}
        except Exception:  # noqa: BLE001 - a full rebuild can repair the primary index later.
            main_indexed_ids = set()
        finally:
            if main_index is not None:
                main_index.close()

    if delta_path.exists():
        existing_delta = None
        try:
            existing_delta = np.load(delta_path, allow_pickle=False)
            existing_chunk_ids = existing_delta["chunk_ids"].astype(np.int64)
            existing_vectors = existing_delta["vectors"].astype(np.float32)
            if existing_vectors.shape[0] != existing_chunk_ids.shape[0]:
                raise ValueError("vector count does not match chunk IDs")
            if existing_vectors.shape[1] != embedder.dimension:
                raise ValueError("vector dimension does not match current embedder")
        except Exception:  # noqa: BLE001 - fall back to a correct rebuild if the local index is corrupt.
            existing_chunk_ids = np.zeros((0,), dtype=np.int64)
            existing_vectors = np.zeros((0, embedder.dimension), dtype=np.float32)
        finally:
            if existing_delta is not None:
                existing_delta.close()
    else:
        existing_chunk_ids = np.zeros((0,), dtype=np.int64)
        existing_vectors = np.zeros((0, embedder.dimension), dtype=np.float32)

    indexed_ids = [int(value) for value in existing_chunk_ids.tolist()]
    live_indexed_ids = _existing_chunk_ids(conn, indexed_ids)
    keep_mask = np.asarray(
        [
            int(chunk_id) in live_indexed_ids and int(chunk_id) not in target_chunk_id_set
            for chunk_id in existing_chunk_ids
        ],
        dtype=bool,
    )
    kept_chunk_ids = existing_chunk_ids[keep_mask]
    kept_vectors = existing_vectors[keep_mask]

    already_indexed_ids = main_indexed_ids | {int(chunk_id) for chunk_id in kept_chunk_ids.tolist()}
    chunks_to_embed = [chunk_id for chunk_id in target_chunk_ids if chunk_id not in already_indexed_ids]
    if max_new_chunks is not None and max_new_chunks >= 0:
        chunks_to_embed = chunks_to_embed[:max_new_chunks]

    new_chunk_ids: list[int] = []
    matrices: list[Any] = []
    batch_ids: list[int] = []
    batch_texts: list[str] = []

    def flush() -> None:
        nonlocal batch_ids, batch_texts
        if not batch_texts:
            return
        matrices.append(embedder.embed(batch_texts))
        new_chunk_ids.extend(batch_ids)
        batch_ids = []
        batch_texts = []

    for row in iter_chunks_by_ids_for_index(conn, chunks_to_embed):
        batch_ids.append(int(row["chunk_id"]))
        batch_texts.append(str(row["text"]))
        if len(batch_texts) >= batch_size:
            flush()
    flush()

    if matrices:
        new_vectors = np.vstack(matrices).astype(np.float32)
        combined_chunk_ids = np.concatenate([kept_chunk_ids, np.asarray(new_chunk_ids, dtype=np.int64)])
        combined_vectors = np.vstack([kept_vectors, new_vectors]) if len(kept_vectors) else new_vectors
    else:
        combined_chunk_ids = kept_chunk_ids
        combined_vectors = kept_vectors

    if len(combined_chunk_ids):
        order = np.argsort(combined_chunk_ids)
        combined_chunk_ids = combined_chunk_ids[order]
        combined_vectors = combined_vectors[order]
    else:
        combined_vectors = np.zeros((0, embedder.dimension), dtype=np.float32)

    tmp_path = delta_path.with_name(delta_path.name + ".tmp")
    with tmp_path.open("wb") as fh:
        np.savez(
            fh,
            chunk_ids=combined_chunk_ids,
            vectors=combined_vectors,
            provider=np.asarray([embedder.provider_name]),
            dimension=np.asarray([embedder.dimension], dtype=np.int64),
            created_at=np.asarray([datetime.now(timezone.utc).isoformat()]),
        )
    tmp_path.replace(delta_path)

    main_count = len(main_indexed_ids)
    return main_count + len(combined_chunk_ids)


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


def count_fts_rows(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0])


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
    chunk_ids: list[int] = []
    for offset in range(0, len(delete_ids), 900):
        batch = delete_ids[offset : offset + 900]
        placeholders = ",".join("?" for _ in batch)
        chunk_ids.extend(
            int(chunk_row["id"])
            for chunk_row in conn.execute(
                f"SELECT id FROM chunks WHERE document_id IN ({placeholders})",
                batch,
            ).fetchall()
        )
    if chunk_ids:
        for offset in range(0, len(chunk_ids), 900):
            batch = chunk_ids[offset : offset + 900]
            placeholders = ",".join("?" for _ in batch)
            conn.execute(f"DELETE FROM chunks_fts WHERE rowid IN ({placeholders})", batch)
    placeholders = ",".join("?" for _ in delete_ids)
    conn.execute(f"DELETE FROM documents WHERE id IN ({placeholders})", delete_ids)
    return len(delete_ids)


def count_rows(conn: sqlite3.Connection) -> tuple[int, int]:
    doc_count = int(conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
    chunk_count = int(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
    return doc_count, chunk_count
