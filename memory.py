import os
import sqlite3
from pathlib import Path

DB_PATH = Path(os.getenv("MICHELLE_DB_PATH", "michelle.db"))

# Only this many past messages are sent to the AI each turn.
# The full history stays in the database; this just keeps prompts fast and cheap.
MAX_HISTORY = int(os.getenv("MAX_HISTORY", "10"))


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # WAL mode avoids "database is locked" errors when uvicorn --reload
    # or overlapping requests touch the DB at the same time.
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_messages_conversation
            ON messages (conversation_id, id)
            """
        )


def get_history(conversation_id: str, limit: int = MAX_HISTORY) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT role, content
            FROM messages
            WHERE conversation_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (conversation_id, limit),
        ).fetchall()

    # Rows come back newest-first; reverse so the AI reads oldest-first.
    return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]


def save_message(conversation_id: str, role: str, content: str) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO messages (conversation_id, role, content)
            VALUES (?, ?, ?)
            """,
            (conversation_id, role, content),
        )
