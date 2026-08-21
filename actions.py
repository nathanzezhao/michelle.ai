"""ACTION engine: actions_log state machine + executors (SPEC-PIPELINE §4–§7).

Mirrors long_term_memory.py conventions: module-level DB_PATH, WAL, init_db(),
additive migrations. One non-terminal row per (user_id, conversation_id) at
any time — create_action() enforces it by cancelling leftovers.
"""

import json
import os
import sqlite3
import subprocess
from pathlib import Path
from uuid import uuid4

# Same DB file as chat + long-term memory; actions get their own table.
DB_PATH = Path(os.getenv("MICHELLE_DB_PATH", "michelle.db"))

# Security-critical (SPEC-PIPELINE §7): risk tiers live HERE and only here.
# No LLM output, env var, or request field may set or override risk.
ACTION_WHITELIST = {
    "open_app": {
        "risk": "low",
        "required_params": ["app_name"],
        "executor": "native",
    },
    "send_email": {
        "risk": "high",
        "required_params": ["recipient", "subject", "body"],
        "executor": "composio",
    },
}

NON_TERMINAL_STATUSES = ("AWAITING_INPUT", "PENDING", "CONFIRMED")
TERMINAL_STATUSES = ("SUCCESS", "FAILED", "CANCELLED")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS actions_log (
                action_id       TEXT PRIMARY KEY,
                user_id         TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                action_type     TEXT NOT NULL,
                payload_json    TEXT NOT NULL,
                status          TEXT NOT NULL,
                created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_actions_open
            ON actions_log (user_id, conversation_id, status)
            """
        )


def _row_to_action(row: sqlite3.Row) -> dict:
    try:
        payload = json.loads(row["payload_json"])
    except json.JSONDecodeError:
        payload = {}
    action_type = row["action_type"]
    entry = ACTION_WHITELIST.get(action_type, {})
    return {
        "action_id": row["action_id"],
        "user_id": row["user_id"],
        "conversation_id": row["conversation_id"],
        "action_type": action_type,
        "status": row["status"],
        # Risk always read from the whitelist, never from stored data (§7).
        "risk": entry.get("risk", "high"),
        "resolved_params": payload.get("resolved_params") or {},
        "missing_params": payload.get("missing_params") or [],
        "error": payload.get("error"),
    }


def _payload_json(action_type: str, resolved: dict, missing: list, error=None) -> str:
    payload = {
        "resolved_params": resolved or {},
        "missing_params": missing or [],
        "risk": ACTION_WHITELIST.get(action_type, {}).get("risk", "high"),
    }
    if error:
        payload["error"] = error
    return json.dumps(payload)


def get_action(action_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM actions_log WHERE action_id = ?", (action_id,)
        ).fetchone()
    return _row_to_action(row) if row else None


def get_open_action(user_id: str, conversation_id: str) -> dict | None:
    """The single non-terminal action for this (user, conversation), if any."""
    placeholders = ",".join("?" for _ in NON_TERMINAL_STATUSES)
    with _connect() as conn:
        row = conn.execute(
            f"""
            SELECT * FROM actions_log
            WHERE user_id = ? AND conversation_id = ?
              AND status IN ({placeholders})
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (user_id, conversation_id, *NON_TERMINAL_STATUSES),
        ).fetchone()
    return _row_to_action(row) if row else None


def create_action(
    user_id: str,
    conversation_id: str,
    action_type: str,
    resolved_params: dict,
    missing_params: list,
    status: str,
) -> dict:
    """Insert a new action row, cancelling any leftover non-terminal row so the
    one-open-action invariant (§4) holds even if a caller forgot to."""
    if action_type not in ACTION_WHITELIST:
        raise ValueError(f"action_type not whitelisted: {action_type}")
    action_id = str(uuid4())
    placeholders = ",".join("?" for _ in NON_TERMINAL_STATUSES)
    with _connect() as conn:
        conn.execute(
            f"""
            UPDATE actions_log
            SET status = 'CANCELLED', updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ? AND conversation_id = ?
              AND status IN ({placeholders})
            """,
            (user_id, conversation_id, *NON_TERMINAL_STATUSES),
        )
        conn.execute(
            """
            INSERT INTO actions_log
                (action_id, user_id, conversation_id, action_type,
                 payload_json, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                action_id,
                user_id,
                conversation_id,
                action_type,
                _payload_json(action_type, resolved_params, missing_params),
                status,
            ),
        )
    return get_action(action_id)


def update_action(
    action_id: str,
    *,
    status: str | None = None,
    resolved_params: dict | None = None,
    missing_params: list | None = None,
    error: str | None = None,
) -> dict | None:
    """Update a NON-terminal row. Terminal states are never mutated (§4)."""
    action = get_action(action_id)
    if action is None or action["status"] in TERMINAL_STATUSES:
        return action
    new_status = status or action["status"]
    resolved = resolved_params if resolved_params is not None else action["resolved_params"]
    missing = missing_params if missing_params is not None else action["missing_params"]
    with _connect() as conn:
        conn.execute(
            """
            UPDATE actions_log
            SET status = ?, payload_json = ?, updated_at = CURRENT_TIMESTAMP
            WHERE action_id = ?
            """,
            (
                new_status,
                _payload_json(action["action_type"], resolved, missing, error),
                action_id,
            ),
        )
    return get_action(action_id)


def cancel_action(action_id: str) -> dict | None:
    return update_action(action_id, status="CANCELLED")


def startup_sweep() -> None:
    """No replay after a restart (§4, decision 7).

    AWAITING_INPUT / PENDING → CANCELLED: nothing was executing, safe to drop.
    CONFIRMED → FAILED "interrupted_by_restart": we crashed mid-execution and
    cannot know if the side effect happened — FAILED is the honest audit state.
    """
    with _connect() as conn:
        conn.execute(
            """
            UPDATE actions_log
            SET status = 'CANCELLED', updated_at = CURRENT_TIMESTAMP
            WHERE status IN ('AWAITING_INPUT', 'PENDING')
            """
        )
        confirmed = conn.execute(
            "SELECT * FROM actions_log WHERE status = 'CONFIRMED'"
        ).fetchall()
        for row in confirmed:
            action = _row_to_action(row)
            conn.execute(
                """
                UPDATE actions_log
                SET status = 'FAILED', payload_json = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE action_id = ?
                """,
                (
                    _payload_json(
                        action["action_type"],
                        action["resolved_params"],
                        action["missing_params"],
                        error="interrupted_by_restart",
                    ),
                    action["action_id"],
                ),
            )


# --- Executors (§5) ---------------------------------------------------------
# ExecResult: {"ok": bool, "detail": str, "error": str | None}


class NativeExecutor:
    """Local macOS actions. Args are passed as a list — user input is never
    interpolated into a shell string."""

    def execute(self, action_type: str, params: dict) -> dict:
        if action_type != "open_app":
            return {"ok": False, "detail": "unsupported native action", "error": "unsupported"}
        app_name = str(params.get("app_name") or "").strip()
        try:
            result = subprocess.run(
                ["open", "-a", app_name],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except Exception as e:
            return {"ok": False, "detail": f"open failed: {e}", "error": "open_failed"}
        if result.returncode == 0:
            return {"ok": True, "detail": f"Opened {app_name}.", "error": None}
        return {
            "ok": False,
            "detail": f"couldn't find an app called {app_name}",
            "error": "app_not_found",
        }


class ComposioExecutor:
    """External actions via Composio (Gmail toolkit first). Lazy client:
    without COMPOSIO_API_KEY this reports not-connected — it never raises at
    startup and the backend boots without the key (§5, decision 6)."""

    def __init__(self):
        self._client = None

    def _get_client(self):
        api_key = os.getenv("COMPOSIO_API_KEY")
        if not api_key:
            return None
        if self._client is None:
            from composio import Composio

            self._client = Composio(api_key=api_key)
        return self._client

    def execute(self, action_type: str, params: dict) -> dict:
        if action_type != "send_email":
            return {"ok": False, "detail": "unsupported composio action", "error": "unsupported"}
        try:
            client = self._get_client()
        except Exception as e:
            return {"ok": False, "detail": f"Composio init failed: {e}", "error": "composio_error"}
        if client is None:
            return {
                "ok": False,
                "detail": "COMPOSIO_API_KEY is not set",
                "error": "composio_not_connected",
            }
        try:
            response = client.tools.execute(
                slug="GMAIL_SEND_EMAIL",
                user_id=os.getenv("COMPOSIO_USER_ID", "default"),
                arguments={
                    "recipient_email": params.get("recipient", ""),
                    "subject": params.get("subject", ""),
                    "body": params.get("body", ""),
                },
                dangerously_skip_version_check=True,
            )
        except Exception as e:
            return {"ok": False, "detail": f"Composio send failed: {e}", "error": "composio_error"}
        if isinstance(response, dict):
            successful = bool(response.get("successful", True))
            error = response.get("error")
        else:
            successful = bool(getattr(response, "successful", True))
            error = getattr(response, "error", None)
        if successful:
            return {"ok": True, "detail": "email sent", "error": None}
        return {"ok": False, "detail": str(error or "Composio send failed"), "error": "composio_error"}


_EXECUTORS = {
    "native": NativeExecutor(),
    "composio": ComposioExecutor(),
}


def execute_action(action_type: str, params: dict) -> dict:
    """Dispatch to the executor named in the whitelist entry — the only path
    from an action_type to a side effect."""
    entry = ACTION_WHITELIST.get(action_type)
    if entry is None:
        return {"ok": False, "detail": "not a whitelisted action", "error": "unsupported"}
    return _EXECUTORS[entry["executor"]].execute(action_type, params)


def confirm_and_execute(action_id: str) -> tuple[str, dict]:
    """CONFIRMED → execute → SUCCESS | FAILED, all within this request (§4).

    Returns (final_status, exec_result). No-ops on terminal rows.
    """
    action = get_action(action_id)
    if action is None or action["status"] in TERMINAL_STATUSES:
        return (
            action["status"] if action else "UNKNOWN",
            {"ok": False, "detail": "task is not open", "error": "not_open"},
        )
    update_action(action_id, status="CONFIRMED")
    result = execute_action(action["action_type"], action["resolved_params"])
    if result["ok"]:
        update_action(action_id, status="SUCCESS")
        return "SUCCESS", result
    update_action(action_id, status="FAILED", error=result.get("error") or "execution_failed")
    return "FAILED", result
