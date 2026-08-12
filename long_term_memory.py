import json
import os
import sqlite3
from pathlib import Path

# Same DB file as regular chat memory; long-term facts live in their own table.
DB_PATH = Path(os.getenv("MICHELLE_DB_PATH", "michelle.db"))


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS long_term_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                fact_key TEXT NOT NULL,
                fact_value TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.5,
                priority TEXT NOT NULL DEFAULT 'high',
                source_conversation_id TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (user_id, fact_key)
            )
            """
        )
        # Older DBs created before priority existed — add the column if missing.
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(long_term_facts)").fetchall()
        }
        if "priority" not in columns:
            conn.execute(
                """
                ALTER TABLE long_term_facts
                ADD COLUMN priority TEXT NOT NULL DEFAULT 'high'
                """
            )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_facts_user
            ON long_term_facts (user_id, fact_key)
            """
        )
        # Candidate facts waiting for the user to say yes/no.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_memories (
                user_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                facts_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, conversation_id)
            )
            """
        )


def _normalize_fact_value(key: str, value: str) -> str:
    """Canonicalize values — names are always title-cased (nathan → Nathan)."""
    value = value.strip()
    if key == "name" and value:
        return " ".join(part.capitalize() for part in value.split())
    return value


def get_facts(user_id: str) -> list[dict]:
    """All durable facts for this user (survive new conversations)."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT fact_key, fact_value, confidence, priority
            FROM long_term_facts
            WHERE user_id = ?
            ORDER BY fact_key ASC
            """,
            (user_id,),
        ).fetchall()

    return [
        {
            "key": row["fact_key"],
            "value": _normalize_fact_value(row["fact_key"], row["fact_value"]),
            "confidence": row["confidence"],
            "priority": row["priority"],
        }
        for row in rows
    ]


def get_fact(user_id: str, fact_key: str) -> str | None:
    key = fact_key.strip().lower().replace(" ", "_")
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT fact_value
            FROM long_term_facts
            WHERE user_id = ? AND fact_key = ?
            """,
            (user_id, key),
        ).fetchone()
    if not row:
        return None
    return _normalize_fact_value(key, row["fact_value"])


def upsert_fact(
    user_id: str,
    fact_key: str,
    fact_value: str,
    confidence: float = 0.5,
    priority: str = "high",
    conversation_id: str | None = None,
) -> None:
    """Save or update a long-term fact for this user (keyed by fact_key)."""
    key = fact_key.strip().lower().replace(" ", "_")
    value = _normalize_fact_value(key, fact_value)
    if not key or not value:
        return
    if key == "name" and value.lower() in {"michelle", "chelle", "michelle.ai"}:
        return

    priority = (priority or "high").strip().lower()
    if priority not in ("high", "medium", "low"):
        priority = "high"

    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO long_term_facts (
                user_id, fact_key, fact_value, confidence, priority,
                source_conversation_id, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id, fact_key) DO UPDATE SET
                fact_value = excluded.fact_value,
                confidence = excluded.confidence,
                priority = excluded.priority,
                source_conversation_id = excluded.source_conversation_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            (user_id, key, value, confidence, priority, conversation_id),
        )


def format_facts(facts: list[dict]) -> str:
    """Turn facts into a short block Michelle can read in her system prompt."""
    if not facts:
        return ""
    lines = [f"- {fact['key']}: {fact['value']}" for fact in facts]
    return "Things you know about this user:\n" + "\n".join(lines)


def has_name(user_id: str) -> bool:
    return bool(get_fact(user_id, "name"))


def delete_fact(user_id: str, fact_key: str) -> bool:
    """Remove one long-term fact. Returns True if a row was deleted."""
    key = fact_key.strip().lower().replace(" ", "_")
    with _connect() as conn:
        cur = conn.execute(
            """
            DELETE FROM long_term_facts
            WHERE user_id = ? AND fact_key = ?
            """,
            (user_id, key),
        )
        return cur.rowcount > 0


def set_pending_memory(
    user_id: str,
    conversation_id: str,
    facts: list[dict],
) -> None:
    """Park candidate facts until the user confirms they want them remembered."""
    if not facts:
        clear_pending_memory(user_id, conversation_id)
        return
    payload = json.dumps(facts)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO pending_memories (user_id, conversation_id, facts_json, created_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id, conversation_id) DO UPDATE SET
                facts_json = excluded.facts_json,
                created_at = CURRENT_TIMESTAMP
            """,
            (user_id, conversation_id, payload),
        )


def get_pending_memory(user_id: str, conversation_id: str) -> list[dict]:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT facts_json
            FROM pending_memories
            WHERE user_id = ? AND conversation_id = ?
            """,
            (user_id, conversation_id),
        ).fetchone()
    if not row:
        return []
    try:
        data = json.loads(row["facts_json"])
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def clear_pending_memory(user_id: str, conversation_id: str) -> None:
    with _connect() as conn:
        conn.execute(
            """
            DELETE FROM pending_memories
            WHERE user_id = ? AND conversation_id = ?
            """,
            (user_id, conversation_id),
        )
