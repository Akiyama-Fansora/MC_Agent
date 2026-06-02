from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import sys

from .chunking import chunk_document
from .cleaners import iter_source_files, load_documents_from_path
from .config import AppConfig, load_config
from .embeddings import make_embedder
from .storage import (
    chunk_ids_for_source_roots,
    connect,
    count_fts_rows,
    count_rows,
    delete_source_documents_not_in,
    init_db,
    rebuild_fts_index,
    rebuild_vector_index,
    replace_document,
    update_vector_index_for_chunks,
)


@dataclass(slots=True)
class IngestStats:
    files_seen: int = 0
    files_loaded: int = 0
    documents_loaded: int = 0
    chunks_written: int = 0
    index_vectors: int = 0
    fts_rows: int = 0
    documents_removed: int = 0
    errors: int = 0
    index_target_chunks: int = 0
    index_pending_chunks: int = 0


def ingest_exports(
    config: AppConfig,
    source_dir: Path | None = None,
    rebuild_index: bool = True,
    limit_files: int | None = None,
    allowed_roots: list[Path] | None = None,
    incremental_index_chunk_limit: int | None = 256,
) -> IngestStats:
    source = source_dir or config.paths.source_dir
    if not source.exists():
        raise FileNotFoundError(
            f"Source directory does not exist: {source}. "
            "Create it and put crawler export files there."
        )
    resolved_allowed_roots = [path.resolve() for path in allowed_roots or []]

    stats = IngestStats()
    conn = connect(config.paths.db_path)
    try:
        init_db(conn)
        seen_source_refs: set[str] = set()
        for path in iter_source_files(source):
            if resolved_allowed_roots:
                resolved_path = path.resolve()
                if not any(resolved_path.is_relative_to(root) for root in resolved_allowed_roots):
                    continue
            if limit_files is not None and stats.files_seen >= limit_files:
                break
            stats.files_seen += 1
            try:
                documents = load_documents_from_path(path, source)
            except Exception as exc:  # noqa: BLE001 - importer should continue on bad files.
                stats.errors += 1
                print(f"[WARN] Failed to load {path}: {exc}", file=sys.stderr)
                continue
            if documents:
                stats.files_loaded += 1
            for document in documents:
                seen_source_refs.add(document.source_ref)
                chunks = chunk_document(
                    document,
                    max_chars=config.chunking.max_chars,
                    overlap_chars=config.chunking.overlap_chars,
                )
                replace_document(conn, document, chunks)
                stats.documents_loaded += 1
                stats.chunks_written += len(chunks)
        if limit_files is None and stats.errors == 0:
            if resolved_allowed_roots:
                for root in resolved_allowed_roots:
                    stats.documents_removed += delete_source_documents_not_in(conn, root, seen_source_refs)
            else:
                stats.documents_removed = delete_source_documents_not_in(conn, source, seen_source_refs)
        conn.commit()
        if resolved_allowed_roots:
            stats.fts_rows = count_fts_rows(conn)
        else:
            stats.fts_rows = rebuild_fts_index(conn)
        conn.commit()
        if rebuild_index:
            embedder = make_embedder(config.embedding)
            if resolved_allowed_roots:
                target_chunk_ids = chunk_ids_for_source_roots(conn, resolved_allowed_roots)
                stats.index_target_chunks = len(target_chunk_ids)
                stats.index_vectors = update_vector_index_for_chunks(
                    conn,
                    config.paths.index_path,
                    embedder,
                    target_chunk_ids,
                    max_new_chunks=incremental_index_chunk_limit,
                )
                if config.paths.index_path.exists():
                    np = __import__("numpy")
                    indexed_ids: set[int] = set()
                    for index_path in [
                        config.paths.index_path,
                        config.paths.index_path.with_name(f"{config.paths.index_path.stem}.delta{config.paths.index_path.suffix}"),
                    ]:
                        if not index_path.exists():
                            continue
                        index_data = None
                        try:
                            index_data = np.load(index_path, allow_pickle=False)
                            indexed_ids.update(int(value) for value in index_data["chunk_ids"].tolist())
                        finally:
                            if index_data is not None:
                                index_data.close()
                    stats.index_pending_chunks = len([chunk_id for chunk_id in target_chunk_ids if chunk_id not in indexed_ids])
            else:
                stats.index_vectors = rebuild_vector_index(conn, config.paths.index_path, embedder)
        return stats
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import local crawler exports into the offline MCagent index.")
    parser.add_argument("--config", help="Path to config JSON. Defaults to config.json.")
    parser.add_argument("--source", help="Override source directory. Defaults to data/crawler_exports.")
    parser.add_argument("--no-index", action="store_true", help="Skip vector index rebuild.")
    parser.add_argument("--limit-files", type=int, help="Only import the first N files.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config(args.config)
    source = Path(args.source).resolve() if args.source else None
    try:
        stats = ingest_exports(
            config,
            source_dir=source,
            rebuild_index=not args.no_index,
            limit_files=args.limit_files,
        )
    except FileNotFoundError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    except sqlite3.Error as exc:
        print(f"[ERROR] SQLite failure: {exc}", file=sys.stderr)
        return 3

    conn = connect(config.paths.db_path)
    try:
        total_docs, total_chunks = count_rows(conn)
    finally:
        conn.close()

    print("Import finished")
    print(f"  source_dir:       {source or config.paths.source_dir}")
    print(f"  files_seen:       {stats.files_seen}")
    print(f"  files_loaded:     {stats.files_loaded}")
    print(f"  documents_loaded: {stats.documents_loaded}")
    print(f"  chunks_written:   {stats.chunks_written}")
    print(f"  fts_rows:         {stats.fts_rows}")
    print(f"  documents_removed:{stats.documents_removed}")
    print(f"  index_vectors:    {stats.index_vectors}")
    print(f"  total_documents:  {total_docs}")
    print(f"  total_chunks:     {total_chunks}")
    print(f"  errors:           {stats.errors}")
    return 0 if stats.errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
