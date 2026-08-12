"""Local doc retrieval (RETRIEVE intent).

Indexes markdown/text files from the docs/ folder into SQLite FTS5.
No Postgres / embeddings required for v1 — swap search() later for vectors.
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

DB_PATH = Path(os.getenv("MICHELLE_DB_PATH", "michelle.db"))
DOCS_DIR = Path(os.getenv("MICHELLE_DOCS_DIR", "docs"))

CHUNK_SIZE = int(os.getenv("RETRIEVE_CHUNK_SIZE", "600"))
CHUNK_OVERLAP = int(os.getenv("RETRIEVE_CHUNK_OVERLAP", "80"))
TOP_K = int(os.getenv("RETRIEVE_TOP_K", "4"))

SUPPORTED_SUFFIXES = {".md", ".txt", ".markdown"}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS doc_files (
                source TEXT PRIMARY KEY,
                mtime REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS doc_chunks_fts
            USING fts5(
                source,
                content,
                tokenize = 'porter unicode61'
            )
            """
        )


def _chunk_text(text: str) -> list[str]:
    text = text.strip()
    if not text:
        return []

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    current = ""

    for para in paragraphs:
        candidate = f"{current}\n\n{para}".strip() if current else para
        if len(candidate) <= CHUNK_SIZE:
            current = candidate
            continue
        if current:
            chunks.append(current)
        if len(para) <= CHUNK_SIZE:
            current = para
        else:
            start = 0
            while start < len(para):
                end = min(start + CHUNK_SIZE, len(para))
                piece = para[start:end].strip()
                if piece:
                    chunks.append(piece)
                if end >= len(para):
                    break
                start = max(0, end - CHUNK_OVERLAP)
            current = ""

    if current:
        chunks.append(current)
    return chunks


def _list_doc_files() -> list[Path]:
    if not DOCS_DIR.is_dir():
        return []
    return [
        path
        for path in sorted(DOCS_DIR.rglob("*"))
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
    ]


def _needs_reindex(conn: sqlite3.Connection, files: list[Path]) -> bool:
    rows = conn.execute("SELECT source, mtime FROM doc_files").fetchall()
    indexed = {row["source"]: row["mtime"] for row in rows}
    current = {
        str(path.relative_to(DOCS_DIR)): path.stat().st_mtime for path in files
    }
    if set(indexed) != set(current):
        return True
    return any(abs(indexed[source] - mtime) > 0.001 for source, mtime in current.items())


def index_docs(force: bool = False) -> int:
    """Read docs/ into SQLite FTS. Returns number of chunks indexed."""
    files = _list_doc_files()
    with _connect() as conn:
        if not force and files and not _needs_reindex(conn, files):
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM doc_chunks_fts"
            ).fetchone()["n"]
            return int(count)

        conn.execute("DELETE FROM doc_chunks_fts")
        conn.execute("DELETE FROM doc_files")

        total = 0
        for path in files:
            source = str(path.relative_to(DOCS_DIR))
            mtime = path.stat().st_mtime
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                text = path.read_text(encoding="utf-8", errors="ignore")

            for chunk in _chunk_text(text):
                conn.execute(
                    "INSERT INTO doc_chunks_fts (source, content) VALUES (?, ?)",
                    (source, chunk),
                )
                total += 1

            conn.execute(
                "INSERT INTO doc_files (source, mtime) VALUES (?, ?)",
                (source, mtime),
            )

        print(
            f"[retrieve] Indexed {total} chunks from {len(files)} file(s) in {DOCS_DIR}/"
        )
        return total


def _fts_query(user_text: str) -> str:
    """Turn free text into a safe FTS5 OR query over meaningful tokens."""
    tokens = re.findall(r"[A-Za-z0-9_]{2,}", user_text.lower())
    stop = {
        "the",
        "and",
        "for",
        "are",
        "was",
        "what",
        "whats",
        "when",
        "where",
        "who",
        "how",
        "why",
        "can",
        "you",
        "our",
        "your",
        "about",
        "with",
        "from",
        "this",
        "that",
        "have",
        "has",
        "does",
        "did",
        "tell",
        "me",
        "please",
        "a",
        "an",
        "is",
        "it",
        "to",
        "of",
        "in",
        "on",
        "do",
        "i",
    }
    kept = [t for t in tokens if t not in stop]
    if not kept:
        kept = tokens[:5] or ["document"]
    parts = [f'"{t}"' for t in kept[:12]]
    return " OR ".join(parts)


def search(query: str, limit: int = TOP_K) -> list[dict]:
    """Return top matching doc chunks for a user question."""
    index_docs()
    fts = _fts_query(query)
    with _connect() as conn:
        try:
            rows = conn.execute(
                """
                SELECT source, content, bm25(doc_chunks_fts) AS score
                FROM doc_chunks_fts
                WHERE doc_chunks_fts MATCH ?
                ORDER BY score
                LIMIT ?
                """,
                (fts, limit),
            ).fetchall()
        except sqlite3.OperationalError as e:
            print(f"[retrieve] FTS query failed ({e}); falling back to LIKE")
            needle = f"%{query.strip()[:80]}%"
            rows = conn.execute(
                """
                SELECT source, content, 0.0 AS score
                FROM doc_chunks_fts
                WHERE content LIKE ?
                LIMIT ?
                """,
                (needle, limit),
            ).fetchall()

    return [
        {
            "source": row["source"],
            "content": row["content"],
            "score": float(row["score"]),
        }
        for row in rows
    ]


_CONTENT_STOP = {
    "the",
    "and",
    "for",
    "are",
    "was",
    "what",
    "whats",
    "when",
    "where",
    "who",
    "how",
    "why",
    "can",
    "you",
    "our",
    "your",
    "about",
    "with",
    "from",
    "this",
    "that",
    "have",
    "has",
    "does",
    "did",
    "tell",
    "please",
}


def _content_tokens(text: str) -> set[str]:
    tokens = set(re.findall(r"[a-z0-9]{4,}", (text or "").lower()))
    return tokens - _CONTENT_STOP


def _relevant_chunks(query: str, chunks: list[dict]) -> list[dict]:
    """Drop FTS hits that don't actually share the question's content words."""
    query_tokens = _content_tokens(query)
    if not query_tokens or not chunks:
        return chunks
    need = 2 if len(query_tokens) >= 2 else 1
    kept = []
    for chunk in chunks:
        overlap = query_tokens & _content_tokens(chunk.get("content", ""))
        if len(overlap) >= need:
            kept.append(chunk)
    return kept


def format_context(chunks: list[dict]) -> str:
    if not chunks:
        return ""
    blocks = []
    for i, chunk in enumerate(chunks, start=1):
        blocks.append(
            f"[{i}] Source: {chunk['source']}\n{chunk['content'].strip()}"
        )
    return "\n\n".join(blocks)


def answer_from_docs(query: str) -> tuple[str, list[dict]]:
    """Search docs and return (context_string, chunks)."""
    chunks = _relevant_chunks(query, search(query))
    return format_context(chunks), chunks
