import os
import sqlite3
from pathlib import Path

DB_PATH = Path(os.getenv("MICHELLE_DB_PATH", "michelle.db"))

# Only this many past messages are sent to the AI each turn.
# The full history stays in the database; this just keeps prompts fast and cheap.
# Long-term facts live in long_term_memory.py and are not limited by this window.
MAX_HISTORY = int(os.getenv("MAX_HISTORY", "10"))


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # WAL mode avoids "database is locked" errors when uvicorn --reload
    # or overlapping requests touch the DB at the same time.
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    """Regular (short-term) chat memory only. Long-term facts: long_term_memory.init_db()."""
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
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(messages)").fetchall()
        }
        if "kind" not in columns:
            conn.execute("ALTER TABLE messages ADD COLUMN kind TEXT")


def get_history(conversation_id: str, limit: int = MAX_HISTORY) -> list[dict]:
    """Regular memory: recent turns for this conversation only."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT role, content, kind
            FROM messages
            WHERE conversation_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (conversation_id, limit),
        ).fetchall()

    # Rows come back newest-first; reverse so the AI reads oldest-first.
    return [
        {
            "role": row["role"],
            "content": row["content"],
            "kind": row["kind"],
        }
        for row in reversed(rows)
    ]


def save_message(
    conversation_id: str,
    role: str,
    content: str,
    kind: str | None = None,
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO messages (conversation_id, role, content, kind)
            VALUES (?, ?, ?, ?)
            """,
            (conversation_id, role, content, kind),
        )
