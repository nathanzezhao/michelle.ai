"""Per-conversation working pad: what this chat is doing right now.

Not durable identity. Not long_term_facts. Not injected into the LLM prompt.
Keyed by (user_id, conversation_id). A new conversation_id starts empty.
"""

import json
import os
import sqlite3
from pathlib import Path

# Same sqlite file as memory.py / actions.py (tests set MICHELLE_DB_PATH first).
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
            CREATE TABLE IF NOT EXISTS session_context (
              user_id TEXT NOT NULL,
              conversation_id TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              PRIMARY KEY (user_id, conversation_id)
            )
            """
        )


def _empty() -> dict:
    return {
        "last_opened": [],
        "last_closed": [],
        "last_quit": [],
        "last_app_names": [],
        "last_action_type": None,
        "last_draft": None,
        "last_draft_pick": None,
    }


def _clean_text(value) -> str:
    return str(value or "").strip()


def _normalize_last_draft(draft) -> dict | None:
    if not isinstance(draft, dict):
        return None
    out = {}
    provider = _clean_text(draft.get("provider"))
    if provider:
        out["provider"] = provider
    remote = _clean_text(
        draft.get("remote_id") or draft.get("gmail_draft_id") or draft.get("mail_draft_id")
    )
    if remote:
        out["remote_id"] = remote
    for key in ("recipient", "subject", "body"):
        val = _clean_text(draft.get(key))
        if val:
            out[key] = val
    if not out:
        return None
    if "provider" not in out and (out.get("remote_id") or out.get("recipient")):
        out["provider"] = "gmail"
    return out


def _slim_pick_candidate(draft) -> dict | None:
    if not isinstance(draft, dict):
        return None
    remote = _clean_text(
        draft.get("remote_id") or draft.get("gmail_draft_id") or draft.get("mail_draft_id")
    )
    recipient = _clean_text(draft.get("recipient"))
    subject = _clean_text(draft.get("subject"))
    body = _clean_text(draft.get("body"))
    snippet = _clean_text(draft.get("snippet")) or body[:160]
    if not remote and not recipient and not subject and not snippet:
        return None
    out = {
        "provider": _clean_text(draft.get("provider")) or "gmail",
        "remote_id": remote or None,
        "recipient": recipient,
        "subject": subject,
        "snippet": snippet[:160],
        "body": body,
    }
    if remote:
        out["gmail_draft_id"] = remote
    return out


def _normalize_last_draft_pick(pick) -> dict | None:
    if not isinstance(pick, dict):
        return None
    candidates = []
    for item in pick.get("candidates") or []:
        slim = _slim_pick_candidate(item)
        if slim:
            candidates.append(slim)
    if not candidates:
        return None
    return {
        "candidates": candidates,
        "query": _clean_text(pick.get("query")),
    }


def get(user_id: str, conversation_id: str) -> dict:
    """Return this conversation's pad. Miss → empty lists / None, not an error."""
    pad = _empty()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT payload_json FROM session_context
            WHERE user_id = ? AND conversation_id = ?
            """,
            (user_id, conversation_id),
        ).fetchone()
    if not row:
        return pad
    try:
        stored = json.loads(row["payload_json"])
    except json.JSONDecodeError:
        return pad
    if not isinstance(stored, dict):
        return pad
    for key in ("last_opened", "last_closed", "last_quit", "last_app_names"):
        value = stored.get(key)
        if isinstance(value, list):
            pad[key] = [str(item).strip() for item in value if str(item or "").strip()]
    action_type = stored.get("last_action_type")
    pad["last_action_type"] = str(action_type).strip() if action_type else None
    pad["last_draft"] = _normalize_last_draft(stored.get("last_draft"))
    pad["last_draft_pick"] = _normalize_last_draft_pick(stored.get("last_draft_pick"))
    return pad


def put(user_id: str, conversation_id: str, payload: dict) -> dict:
    pad = _empty()
    if isinstance(payload, dict):
        pad.update(payload)
    pad["last_draft"] = _normalize_last_draft(pad.get("last_draft"))
    pad["last_draft_pick"] = _normalize_last_draft_pick(pad.get("last_draft_pick"))
    blob = json.dumps(pad)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO session_context (user_id, conversation_id, payload_json, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT (user_id, conversation_id) DO UPDATE SET
              payload_json = excluded.payload_json,
              updated_at = CURRENT_TIMESTAMP
            """,
            (user_id, conversation_id, blob),
        )
    return pad


def _clean_names(app_names) -> list[str]:
    names = []
    for item in app_names or []:
        name = str(item or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def record_action(
    user_id: str,
    conversation_id: str,
    action_type: str,
    app_names=None,
    last_draft=None,
    clear_draft: bool = False,
) -> dict:
    """Merge this settled action into the conversation pad."""
    pad = get(user_id, conversation_id)
    kind = str(action_type or "").strip()
    names = _clean_names(app_names)
    if kind in ("open_app", "close_app", "quit_app"):
        if not names:
            return pad
        if kind == "open_app":
            pad["last_opened"] = names
        elif kind == "close_app":
            pad["last_closed"] = names
        else:
            pad["last_quit"] = names
        pad["last_app_names"] = names
        pad["last_action_type"] = kind
        pad["last_draft_pick"] = None
    elif kind == "send_email":
        pad["last_action_type"] = kind
        if clear_draft:
            pad["last_draft"] = None
        else:
            incoming = _normalize_last_draft(last_draft)
            if incoming:
                merged = dict(pad.get("last_draft") or {})
                merged.update(incoming)
                pad["last_draft"] = _normalize_last_draft(merged)
    else:
        return pad
    return put(user_id, conversation_id, pad)


def set_draft_pick(user_id: str, conversation_id: str, pick) -> dict:
    pad = get(user_id, conversation_id)
    pad["last_draft_pick"] = _normalize_last_draft_pick(pick)
    return put(user_id, conversation_id, pad)


def clear_draft_pick(user_id: str, conversation_id: str) -> dict:
    return set_draft_pick(user_id, conversation_id, None)


def pad_draft_stash(user_id: str, conversation_id: str) -> dict | None:
    """This-chat letter for generic resume. None if the pad has no draft."""
    draft = get(user_id, conversation_id).get("last_draft")
    return _normalize_last_draft(draft)
