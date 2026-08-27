"""ACTION engine: actions_log state machine + executors (SPEC-PIPELINE §4–§7).

Mirrors long_term_memory.py conventions: module-level DB_PATH, WAL, init_db(),
additive migrations. One non-terminal row per (user_id, conversation_id) at
any time — create_action() enforces it by cancelling leftovers.
"""

import base64
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import uuid4

_UNSET = object()

# One page of the Drafts folder. Do not paginate with page_token.
LIST_RECENT_LIMIT = 50
LIST_DRAFTS_BACKOFF_S = 1.0

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
    provider, remote, gmail_id = _sync_mail_ids(
        payload.get("mail_provider"),
        payload.get("mail_draft_id"),
        payload.get("gmail_draft_id"),
    )
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
        "queue": payload.get("queue") or [],
        "gmail_draft_id": gmail_id,
        "gmail_draft_kept": bool(payload.get("gmail_draft_kept")),
        "mail_provider": provider,
        "mail_draft_id": remote,
    }


def _sync_mail_ids(mail_provider, mail_draft_id, gmail_draft_id):
    """Keep mail_draft_id and gmail_draft_id aligned when the provider is gmail."""
    provider = (mail_provider or "").strip().lower() or None
    remote = mail_draft_id or None
    gmail_id = gmail_draft_id or None
    if remote or gmail_id:
        if not provider:
            provider = "gmail"
        if provider == "gmail":
            remote = remote or gmail_id
            gmail_id = remote
        else:
            gmail_id = None
    else:
        remote = None
        gmail_id = None
    return provider, remote, gmail_id


def _coalesce_mail_fields(
    action,
    mail_provider=_UNSET,
    mail_draft_id=_UNSET,
    gmail_draft_id=_UNSET,
):
    provider = action.get("mail_provider") if mail_provider is _UNSET else mail_provider
    remote = action.get("mail_draft_id") if mail_draft_id is _UNSET else mail_draft_id
    gmail_id = action.get("gmail_draft_id") if gmail_draft_id is _UNSET else gmail_draft_id
    if (
        gmail_draft_id is not _UNSET
        and not gmail_id
        and mail_draft_id is _UNSET
        and (provider or "gmail") == "gmail"
    ):
        remote = None
    if (
        mail_draft_id is not _UNSET
        and not remote
        and gmail_draft_id is _UNSET
        and (provider or "gmail") == "gmail"
    ):
        gmail_id = None
    return _sync_mail_ids(provider, remote, gmail_id)


def _payload_json(
    action_type: str,
    resolved: dict,
    missing: list,
    error=None,
    queue=None,
    gmail_draft_id=None,
    gmail_draft_kept=False,
    mail_provider=None,
    mail_draft_id=None,
) -> str:
    provider, remote, gmail_id = _sync_mail_ids(
        mail_provider, mail_draft_id, gmail_draft_id
    )
    payload = {
        "resolved_params": resolved or {},
        "missing_params": missing or [],
        "risk": ACTION_WHITELIST.get(action_type, {}).get("risk", "high"),
    }
    if error:
        payload["error"] = error
    if queue:
        payload["queue"] = queue
    if provider and remote:
        payload["mail_provider"] = provider
    if remote:
        payload["mail_draft_id"] = remote
    if gmail_id:
        payload["gmail_draft_id"] = gmail_id
    if gmail_draft_kept:
        payload["gmail_draft_kept"] = True
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
    queue: list | None = None,
    gmail_draft_id: str | None = None,
    mail_provider: str | None = None,
    mail_draft_id: str | None = None,
) -> dict:
    """Insert a new action row, cancelling any leftover non-terminal row so the
    one-open-action invariant (§4) holds even if a caller forgot to."""
    if action_type not in ACTION_WHITELIST:
        raise ValueError(f"action_type not whitelisted: {action_type}")
    leftover = get_open_action(user_id, conversation_id)
    if leftover:
        delete_gmail_draft(leftover)
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
                _payload_json(
                    action_type,
                    resolved_params,
                    missing_params,
                    queue=queue,
                    gmail_draft_id=gmail_draft_id,
                    mail_provider=mail_provider,
                    mail_draft_id=mail_draft_id,
                ),
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
    queue: list | None = None,
    gmail_draft_id=_UNSET,
    gmail_draft_kept=_UNSET,
    mail_provider=_UNSET,
    mail_draft_id=_UNSET,
) -> dict | None:
    """Update a NON-terminal row. Terminal states are never mutated (§4)."""
    action = get_action(action_id)
    if action is None or action["status"] in TERMINAL_STATUSES:
        return action
    new_status = status or action["status"]
    resolved = resolved_params if resolved_params is not None else action["resolved_params"]
    missing = missing_params if missing_params is not None else action["missing_params"]
    kept_queue = queue if queue is not None else action.get("queue")
    provider, remote, draft_id = _coalesce_mail_fields(
        action,
        mail_provider=mail_provider,
        mail_draft_id=mail_draft_id,
        gmail_draft_id=gmail_draft_id,
    )
    kept = (
        action.get("gmail_draft_kept")
        if gmail_draft_kept is _UNSET
        else bool(gmail_draft_kept)
    )
    with _connect() as conn:
        conn.execute(
            """
            UPDATE actions_log
            SET status = ?, payload_json = ?, updated_at = CURRENT_TIMESTAMP
            WHERE action_id = ?
            """,
            (
                new_status,
                _payload_json(
                    action["action_type"],
                    resolved,
                    missing,
                    error,
                    queue=kept_queue,
                    gmail_draft_id=draft_id,
                    gmail_draft_kept=kept,
                    mail_provider=provider,
                    mail_draft_id=remote,
                ),
                action_id,
            ),
        )
    return get_action(action_id)


def patch_payload(action_id: str, **fields) -> dict | None:
    """Write payload flags on any row, including CANCELLED (draft keep/clear)."""
    action = get_action(action_id)
    if action is None:
        return None
    provider, remote, draft_id = _coalesce_mail_fields(
        action,
        mail_provider=fields["mail_provider"] if "mail_provider" in fields else _UNSET,
        mail_draft_id=fields["mail_draft_id"] if "mail_draft_id" in fields else _UNSET,
        gmail_draft_id=fields["gmail_draft_id"] if "gmail_draft_id" in fields else _UNSET,
    )
    kept = (
        bool(fields["gmail_draft_kept"])
        if "gmail_draft_kept" in fields
        else bool(action.get("gmail_draft_kept"))
    )
    with _connect() as conn:
        conn.execute(
            """
            UPDATE actions_log
            SET payload_json = ?, updated_at = CURRENT_TIMESTAMP
            WHERE action_id = ?
            """,
            (
                _payload_json(
                    action["action_type"],
                    action["resolved_params"],
                    action["missing_params"],
                    error=action.get("error"),
                    queue=action.get("queue"),
                    gmail_draft_id=draft_id,
                    gmail_draft_kept=kept,
                    mail_provider=provider,
                    mail_draft_id=remote,
                ),
                action_id,
            ),
        )
    return get_action(action_id)


def cancel_action(action_id: str, *, discard_gmail: bool = True) -> dict | None:
    action = get_action(action_id)
    if discard_gmail:
        delete_gmail_draft(action)
        return update_action(action_id, status="CANCELLED", gmail_draft_id=None)
    return update_action(action_id, status="CANCELLED")


def get_resumable_gmail_draft(user_id: str, conversation_id: str) -> dict | None:
    """Latest cancelled send_email that still has a Gmail draft to reopen."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM actions_log
            WHERE user_id = ? AND conversation_id = ?
              AND action_type = 'send_email'
              AND status = 'CANCELLED'
            ORDER BY updated_at DESC
            LIMIT 20
            """,
            (user_id, conversation_id),
        ).fetchall()
    for row in rows:
        action = _row_to_action(row)
        if action.get("gmail_draft_id") and not action.get("gmail_draft_kept"):
            return action
    return None


_DRAFT_MATCH_STOP = {
    "send",
    "that",
    "this",
    "the",
    "a",
    "an",
    "to",
    "for",
    "me",
    "please",
    "pls",
    "draft",
    "email",
    "e-mail",
    "one",
    "about",
    "finish",
    "continue",
    "resume",
    "open",
    "my",
    "it",
    "and",
    "or",
    "of",
    "on",
    "can",
    "you",
    "pull",
    "show",
    "get",
    "find",
    "up",
    "just",
    "like",
    "want",
    "wanna",
    "gonna",
    "could",
    "would",
    "will",
    "here",
    "let",
    "see",
    "if",
    "was",
    "is",
    "are",
    "be",
}

HYDRATE_GET_CAP = 8
_DRAFT_SNIPPET_LEN = 160


def _draft_tokens(text: str) -> list[str]:
    return [
        tok
        for tok in re.findall(r"[a-z0-9@.+\-]+", (text or "").lower())
        if tok not in _DRAFT_MATCH_STOP and len(tok) >= 2
    ]


def match_draft(utterance: str, drafts: list[dict], *, resume: bool) -> dict:
    """Word-match a description against mailbox / local drafts.

    status: none | one | newest | hit | ambiguous | miss

    Generic resume (no useful tokens) with several drafts takes drafts[0]
    after the caller sorted newest-first — not ambiguous of the whole page.
    Distinctive tokens that miss every blob are a miss, not a filler-word tie.
    """
    if not drafts:
        return {"status": "none"}
    tokens = _draft_tokens(utterance)
    scored: list[tuple[int, dict]] = []
    for draft in drafts:
        blob = _draft_blob(draft)
        hits = sum(1 for tok in tokens if tok in blob)
        if hits:
            scored.append((hits, draft))
    scored.sort(key=lambda item: -item[0])
    if scored:
        best = scored[0][0]
        winners = [draft for hits, draft in scored if hits == best]
        if len(winners) == 1:
            return {"status": "hit", "draft": winners[0]}
        return {"status": "ambiguous", "candidates": winners}
    if tokens:
        return {"status": "miss" if resume else "none"}
    if resume and len(drafts) == 1:
        return {"status": "one", "draft": drafts[0]}
    if resume and len(drafts) > 1:
        return {"status": "newest", "draft": drafts[0]}
    if resume:
        return {"status": "miss"}
    return {"status": "none"}


def _draft_blob(draft: dict) -> str:
    return " ".join(
        [
            str(draft.get("recipient") or ""),
            str(draft.get("subject") or ""),
            str(draft.get("body") or ""),
            str(draft.get("snippet") or ""),
        ]
    ).lower()


def _draft_has_text(draft: dict) -> bool:
    return bool(
        str(draft.get("body") or "").strip()
        or str(draft.get("snippet") or "").strip()
    )


def _copy_draft(draft: dict) -> dict:
    return dict(draft)


def hydrate_drafts_for_pick(utterance: str, drafts: list[dict]) -> list[dict]:
    """GET thin list rows when the query has real words to match.

    Resume only. Caps extra GETs. Does not run on composer keystroke.
    """
    out = [_copy_draft(d) for d in drafts]
    if not _draft_tokens(utterance):
        return out
    used = 0
    for draft in out:
        if used >= HYDRATE_GET_CAP:
            break
        if _draft_has_text(draft):
            continue
        remote = draft.get("remote_id") or draft.get("gmail_draft_id")
        if not remote:
            continue
        loaded = load_draft(draft.get("provider") or "gmail", str(remote))
        used += 1
        if not loaded.get("ok"):
            continue
        params = loaded.get("resolved_params") or {}
        for key in ("recipient", "subject", "body"):
            val = str(params.get(key) or "").strip()
            if val:
                draft[key] = val
        if draft.get("body") and not draft.get("snippet"):
            draft["snippet"] = str(draft["body"])[:_DRAFT_SNIPPET_LEN]
    return out


def _intent_uses_rules() -> bool:
    mode = (os.getenv("INTENT_MODE") or "llm").lower()
    return mode in ("rules", "mock")


def _llm_pick_ids(utterance: str, drafts: list[dict]) -> list[str] | None:
    """Ask the intent LLM which listed ids match. None = call failed."""
    listed = []
    allowed: set[str] = set()
    for draft in drafts:
        rid = str(draft.get("remote_id") or draft.get("gmail_draft_id") or "")
        if not rid:
            continue
        allowed.add(rid)
        snippet = (
            str(draft.get("snippet") or draft.get("body") or "")[:_DRAFT_SNIPPET_LEN]
        )
        listed.append(
            {
                "id": rid,
                "to": str(draft.get("recipient") or ""),
                "subject": str(draft.get("subject") or ""),
                "snippet": snippet,
            }
        )
    if not listed:
        return []
    prompt = (
        "The user wants one mailbox draft reopened. Pick from the list only.\n"
        "Do not invent an id, to, subject, or body.\n"
        "If none match the user's meaning, return {\"ids\": []}.\n"
        "If several still fit equally, return all of those ids.\n"
        "If exactly one fits, return that one id.\n\n"
        f"User said: {utterance}\n\n"
        f"Drafts: {json.dumps(listed, ensure_ascii=False)}\n\n"
        'Reply with ONLY JSON: {"ids": ["id", ...]}'
    )
    try:
        from intent import _llm_json

        raw = _llm_json(prompt)
    except Exception as e:
        print(f"draft pick LLM failed ({e})")
        return None
    ids = raw.get("ids") if isinstance(raw, dict) else None
    if not isinstance(ids, list):
        one = raw.get("id") if isinstance(raw, dict) else None
        ids = [one] if one else []
    picked = []
    seen: set[str] = set()
    for item in ids:
        rid = str(item or "").strip()
        if rid in allowed and rid not in seen:
            seen.add(rid)
            picked.append(rid)
    return picked


def pick_draft(utterance: str, drafts: list[dict], *, resume: bool) -> dict:
    """Rules token-match, or live LLM pick among hydrated mailbox drafts."""
    if not drafts:
        return {"status": "none"}
    shelf = hydrate_drafts_for_pick(utterance, drafts)
    by_id = {
        str(d.get("remote_id") or d.get("gmail_draft_id") or ""): d
        for d in shelf
        if d.get("remote_id") or d.get("gmail_draft_id")
    }
    token = match_draft(utterance, shelf, resume=resume)
    if _intent_uses_rules():
        return token
    if not _draft_tokens(utterance):
        return token
    picked_ids = _llm_pick_ids(utterance, shelf)
    if picked_ids is None:
        return token
    chosen = [by_id[i] for i in picked_ids if i in by_id]
    if len(chosen) == 1:
        return {"status": "hit", "draft": chosen[0]}
    if len(chosen) > 1:
        return {"status": "ambiguous", "candidates": chosen}
    if resume:
        return {"status": "miss"}
    return {"status": "none"}


def match_gmail_draft(utterance: str, drafts: list[dict], *, resume: bool) -> dict:
    """Alias for match_draft (old name)."""
    return match_draft(utterance, drafts, resume=resume)


def consume_gmail_draft(draft_id: str | None) -> None:
    """After a send, drop the id so the letter is not matched again."""
    if not draft_id:
        return
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM actions_log WHERE action_type = 'send_email'"
        ).fetchall()
    for row in rows:
        action = _row_to_action(row)
        if action.get("gmail_draft_id") == draft_id:
            patch_payload(action["action_id"], gmail_draft_id=None)


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
                        gmail_draft_id=action.get("gmail_draft_id"),
                        gmail_draft_kept=action.get("gmail_draft_kept"),
                        mail_provider=action.get("mail_provider"),
                        mail_draft_id=action.get("mail_draft_id"),
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


def platform_api_key() -> str | None:
    """Platform project key only. A For You consumer key (`ck_...`) is the
    wrong product and would 401 against the SDK — treat it as not connected."""
    api_key = (os.getenv("COMPOSIO_API_KEY") or "").strip()
    if not api_key or api_key.startswith("ck_"):
        return None
    return api_key


def _gmail_connect_link(client, user_id: str) -> str | None:
    """Hosted Connect Link for Gmail. Uses session.authorize (current API)."""
    try:
        session = client.create(user_id=user_id, toolkits=["gmail"])
        req = session.authorize("gmail")
        return getattr(req, "redirect_url", None)
    except Exception:
        return None


def _not_connected_result(client, user_id: str, detail: str) -> dict:
    result = {
        "ok": False,
        "detail": detail,
        "error": "composio_not_connected",
    }
    link = _gmail_connect_link(client, user_id)
    if link:
        result["connect_link"] = link
    return result


def _stage_attachments(paths) -> list[str]:
    """Copy user-picked files into Composio's default upload allowlist."""
    staged = []
    dest_dir = Path.home() / ".composio" / "temp" / "michelle"
    dest_dir.mkdir(parents=True, exist_ok=True)
    for path in paths or []:
        if not isinstance(path, str) or not os.path.isfile(path):
            continue
        name = Path(path).name or "attachment"
        dest = dest_dir / name
        if dest.exists():
            dest = dest_dir / f"{uuid4().hex[:8]}-{name}"
        shutil.copy2(path, dest)
        staged.append(str(dest))
    return staged


def _maybe_json(value):
    """Parse a JSON string. Recurses through quoted JSON; leaves base64 alone."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in '{["':
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def _coerce_tree(value, depth: int = 0):
    """Recursively JSON-parse stringified `data` / payload wrappers."""
    if depth > 8:
        return value
    value = _maybe_json(value)
    if isinstance(value, str) and depth < 8:
        again = _maybe_json(value)
        if again is not value:
            return _coerce_tree(again, depth + 1)
    if isinstance(value, dict):
        return {k: _coerce_tree(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_coerce_tree(v, depth + 1) for v in value]
    return value


def _as_dict(value) -> dict:
    value = _coerce_tree(value)
    if isinstance(value, dict):
        return value
    return {}


def _extract_draft_id(response) -> str | None:
    if not isinstance(response, dict):
        return None
    for key in ("draft_id", "id"):
        val = response.get(key)
        if val:
            return str(val)
    data = _as_dict(response.get("data"))
    for key in ("draft_id", "id", "draftId"):
        val = data.get(key)
        if val:
            return str(val)
    for nest in ("draft", "message"):
        inner = data.get(nest)
        if isinstance(inner, dict) and inner.get("id"):
            return str(inner["id"])
    return None


def _header_map(headers) -> dict:
    out = {}
    for item in headers or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip().lower()
        value = str(item.get("value") or "").strip()
        if name and value:
            out[name] = value
    return out


def _decode_gmail_b64(data_b64) -> str:
    if not data_b64 or not isinstance(data_b64, str):
        return ""
    try:
        pad = "=" * ((4 - len(data_b64) % 4) % 4)
        return base64.urlsafe_b64decode(data_b64 + pad).decode(
            "utf-8", errors="replace"
        )
    except Exception:
        return ""


def _strip_html_tags(text: str) -> str:
    cleaned = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text or "")
    cleaned = re.sub(r"(?s)<[^>]+>", " ", cleaned)
    cleaned = html.unescape(cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _iter_parts(node):
    children = node.get("parts") if isinstance(node, dict) else None
    if isinstance(children, str):
        children = _maybe_json(children)
    if isinstance(children, list):
        for child in children:
            yield child


def _mime_text(part) -> str:
    """Walk MIME parts recursively. Prefer text/plain; else strip text/html."""
    plains: list[str] = []
    htmls: list[str] = []

    def walk(node):
        if isinstance(node, str):
            node = _maybe_json(node)
        if not isinstance(node, dict):
            return
        mime = str(node.get("mimeType") or node.get("mime_type") or "").strip().lower()
        mime = mime.split(";", 1)[0].strip()
        body = node.get("body")
        if isinstance(body, str):
            body = _maybe_json(body)
        raw = ""
        if isinstance(body, dict):
            data = body.get("data")
            if isinstance(data, str) and data.strip().startswith("{"):
                nested = _maybe_json(data)
                if isinstance(nested, dict):
                    data = nested.get("data")
            raw = _decode_gmail_b64(data) if isinstance(data, str) else ""
        elif isinstance(body, str) and body.strip() and not mime.startswith("multipart/"):
            raw = body
        if raw:
            if mime == "text/html":
                htmls.append(raw)
            elif not mime.startswith("multipart/"):
                plains.append(raw)
        for child in _iter_parts(node):
            walk(child)

    walk(part)
    if plains:
        return "\n".join(p.strip() for p in plains if p.strip()).strip()
    if htmls:
        return "\n".join(_strip_html_tags(h) for h in htmls if h).strip()
    return ""


def _fields_from_rfc2822(raw_b64) -> dict:
    """Decode Gmail format=raw (base64url RFC 2822) into to/subject/body."""
    if not isinstance(raw_b64, str) or not raw_b64.strip():
        return {}
    text = _decode_gmail_b64(raw_b64)
    if not text.strip() and "\n" in raw_b64:
        text = raw_b64
    if not text.strip():
        return {}
    try:
        from email import message_from_string
        from email.policy import default as email_policy

        msg = message_from_string(text, policy=email_policy)
    except Exception:
        return {}
    fields: dict = {}
    to = msg.get("to")
    subject = msg.get("subject")
    if to:
        fields["recipient"] = str(to).strip()
    if subject:
        fields["subject"] = str(subject).strip()
    plains: list[str] = []
    htmls: list[str] = []
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        ctype = (part.get_content_type() or "").lower()
        if ctype.startswith("multipart/"):
            continue
        try:
            content = part.get_content()
        except Exception:
            payload = part.get_payload(decode=True)
            if isinstance(payload, bytes):
                content = payload.decode("utf-8", errors="replace")
            else:
                content = str(payload or "")
        if not isinstance(content, str):
            content = str(content)
        if ctype == "text/html":
            htmls.append(content)
        elif ctype.startswith("text/"):
            plains.append(content)
    body = "\n".join(p.strip() for p in plains if p.strip()).strip()
    if not body:
        body = "\n".join(_strip_html_tags(h) for h in htmls if h).strip()
    if body:
        fields["body"] = body
    return fields


def _walk_all_mime(node):
    if isinstance(node, str):
        node = _maybe_json(node)
    if not isinstance(node, dict):
        return
    yield node
    for child in _iter_parts(node):
        yield from _walk_all_mime(child)


def _walk_mime_nodes(response):
    coerced = _coerce_tree(response) if isinstance(response, dict) else {}
    if not isinstance(coerced, dict):
        return
    for src in _draft_blobs(coerced):
        yield from _walk_all_mime(src)


def _has_inline_part_data(response) -> bool:
    for node in _walk_mime_nodes(response):
        body = node.get("body")
        if isinstance(body, dict) and body.get("data"):
            return True
    return False


def _has_attachment_stubs(response) -> bool:
    for node in _walk_mime_nodes(response):
        body = node.get("body") if isinstance(node, dict) else None
        if not isinstance(body, dict):
            continue
        att = body.get("attachmentId") or body.get("attachment_id")
        if att and not body.get("data"):
            return True
    return False


def _should_retry_raw(response, fields: dict) -> bool:
    """Thin Gmail payload or attachmentId-only parts → GET format=raw."""
    if _has_inline_part_data(response):
        return False
    if str((fields or {}).get("body") or "").strip() and not _has_attachment_stubs(response):
        return False
    return True


def _body_content(value) -> str:
    """String body, Gmail body.data, or Graph-style body.content / uniqueBody.content."""
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    content = value.get("content")
    if isinstance(content, str) and content.strip():
        kind = str(
            value.get("contentType") or value.get("content_type") or ""
        ).lower()
        if "html" in kind:
            return _strip_html_tags(content)
        return content.strip()
    return _decode_gmail_b64(value.get("data")).strip()


def _draft_blobs(response: dict) -> list[dict]:
    blobs: list[dict] = [response]
    data = _as_dict(response.get("data"))
    if data:
        blobs.append(data)
        nested = _as_dict(data.get("data"))
        if nested:
            blobs.append(nested)
    extra: list[dict] = []
    for src in blobs:
        msg = src.get("message")
        if isinstance(msg, dict):
            extra.append(msg)
        payload = src.get("payload")
        if isinstance(payload, dict):
            extra.append(payload)
        if isinstance(msg, dict) and isinstance(msg.get("payload"), dict):
            extra.append(msg["payload"])
    blobs.extend(extra)
    return blobs


def _extract_draft_fields(response) -> dict:
    """Pull to/subject/body out of Gmail MIME or Outlook Graph-shaped payloads."""
    response = _coerce_tree(response)
    if not isinstance(response, dict):
        return {}
    fields: dict = {}
    snippet = ""
    for src in _draft_blobs(response):
        headers = _header_map(src.get("headers"))
        payload = src.get("payload") if isinstance(src.get("payload"), dict) else None
        if payload:
            headers = {**_header_map(payload.get("headers")), **headers}
        if "to" in headers and "recipient" not in fields:
            fields["recipient"] = headers["to"]
        if "subject" in headers and "subject" not in fields:
            fields["subject"] = headers["subject"]
        for src_key, dest in (
            ("recipient_email", "recipient"),
            ("recipient", "recipient"),
            ("to", "recipient"),
            ("subject", "subject"),
        ):
            val = src.get(src_key)
            if dest not in fields and isinstance(val, str) and val.strip():
                fields[dest] = val.strip()
        if not snippet:
            for key in ("snippet", "bodyPreview", "body_preview"):
                val = src.get(key)
                if isinstance(val, str) and val.strip():
                    snippet = val.strip()
                    break
        if "body" not in fields:
            for key in ("body", "message_body", "uniqueBody", "unique_body"):
                got = _body_content(src.get(key)) if key in src else ""
                if got:
                    fields["body"] = got
                    break
        if "body" not in fields:
            mime_text = _mime_text(payload or src)
            if mime_text:
                fields["body"] = mime_text
        if isinstance(src.get("raw"), str) and src.get("raw").strip():
            rfc = _fields_from_rfc2822(src["raw"])
            for key, val in rfc.items():
                if val and not fields.get(key):
                    fields[key] = val
    if "body" not in fields and snippet:
        fields["body"] = snippet
    if snippet:
        fields["snippet"] = snippet
    return {k: v for k, v in fields.items() if v}


def gmail_draft_ready(params: dict | None) -> bool:
    """Composio create needs a real To and subject or body."""
    params = params or {}
    recipient = str(params.get("recipient") or "").strip()
    subject = str(params.get("subject") or "").strip()
    body = str(params.get("body") or "").strip()
    return "@" in recipient and bool(subject or body)


class ComposioExecutor:
    """External actions via Composio (Gmail toolkit first). Lazy client:
    without a Platform `COMPOSIO_API_KEY` this reports not-connected — it never
    raises at startup and the backend boots without the key (§5, decision 6)."""

    def __init__(self):
        self._client = None
        self._api_key = None

    def _get_client(self, *, allow_files: bool = False):
        api_key = platform_api_key()
        if not api_key:
            return None
        if self._client is None or self._api_key != api_key or allow_files:
            from composio import Composio

            if allow_files:
                return Composio(dangerously_allow_auto_upload_download_files=True)
            self._client = Composio()
            self._api_key = api_key
        return self._client

    def _not_connected(self, client=None, detail: str | None = None) -> dict:
        raw = (os.getenv("COMPOSIO_API_KEY") or "").strip()
        if detail is None:
            detail = (
                "COMPOSIO_API_KEY is a For You consumer key; Michelle needs a Platform project API key"
                if raw.startswith("ck_")
                else "COMPOSIO_API_KEY is not set"
            )
        if client is None:
            return {
                "ok": False,
                "detail": detail,
                "error": "composio_not_connected",
            }
        return _not_connected_result(client, os.getenv("COMPOSIO_USER_ID", "default"), detail)

    def _run_tool(self, slug: str, arguments: dict, *, allow_files: bool = False) -> dict:
        try:
            client = self._get_client(allow_files=allow_files)
        except Exception as e:
            return {"ok": False, "detail": f"Composio init failed: {e}", "error": "composio_error"}
        if client is None:
            return self._not_connected()
        execute_kwargs = {
            "slug": slug,
            "user_id": os.getenv("COMPOSIO_USER_ID", "default"),
            "arguments": arguments,
            "dangerously_skip_version_check": True,
        }
        account_id = (os.getenv("COMPOSIO_CONNECTED_ACCOUNT_ID") or "").strip()
        if account_id:
            execute_kwargs["connected_account_id"] = account_id
        user_id = execute_kwargs["user_id"]
        try:
            response = client.tools.execute(**execute_kwargs)
        except Exception as e:
            detail = str(e)
            lowered = detail.lower()
            if "no connected account" in lowered or "not connected" in lowered:
                return _not_connected_result(client, user_id, detail)
            return {"ok": False, "detail": f"Composio {slug} failed: {e}", "error": "composio_error"}
        if isinstance(response, dict):
            successful = bool(response.get("successful", True))
            error = response.get("error")
        else:
            successful = bool(getattr(response, "successful", True))
            error = getattr(response, "error", None)
            response = {
                "successful": successful,
                "error": error,
                "data": getattr(response, "data", None),
            }
        if successful:
            return {"ok": True, "detail": slug, "error": None, "raw": response}
        detail = str(error or f"Composio {slug} failed")
        lowered = detail.lower()
        if "not connected" in lowered or "no connected account" in lowered:
            return _not_connected_result(client, user_id, detail)
        return {"ok": False, "detail": detail, "error": "composio_error", "raw": response}

    def _mail_arguments(self, params: dict, *, include_attachments: bool) -> dict:
        arguments = {
            "recipient_email": params.get("recipient", ""),
            "subject": params.get("subject", ""),
            "body": params.get("body", ""),
            "is_html": False,
        }
        if include_attachments:
            attachments = _stage_attachments(params.get("attachments"))
            if attachments:
                arguments["attachment"] = (
                    attachments[0] if len(attachments) == 1 else attachments
                )
        return arguments

    def execute(self, action_type: str, params: dict) -> dict:
        if action_type != "send_email":
            return {"ok": False, "detail": "unsupported composio action", "error": "unsupported"}
        include_files = bool(params.get("attachments"))
        arguments = self._mail_arguments(params, include_attachments=include_files)
        result = self._run_tool(
            "GMAIL_SEND_EMAIL", arguments, allow_files=include_files
        )
        if result.get("ok"):
            result["detail"] = "email sent"
        return result

    def create_draft(self, params: dict) -> dict:
        include_files = bool(params.get("attachments"))
        result = self._run_tool(
            "GMAIL_CREATE_EMAIL_DRAFT",
            self._mail_arguments(params, include_attachments=include_files),
            allow_files=include_files,
        )
        if not result.get("ok"):
            return result
        draft_id = _extract_draft_id(result.get("raw"))
        if not draft_id:
            return {
                "ok": False,
                "detail": "Composio create draft returned no draft_id",
                "error": "composio_error",
            }
        result["draft_id"] = draft_id
        result["detail"] = "draft created"
        return result

    def update_draft(self, draft_id: str, params: dict) -> dict:
        include_files = bool(params.get("attachments"))
        arguments = self._mail_arguments(params, include_attachments=include_files)
        arguments["draft_id"] = draft_id
        result = self._run_tool(
            "GMAIL_UPDATE_DRAFT", arguments, allow_files=include_files
        )
        if result.get("ok"):
            result["draft_id"] = draft_id
            result["detail"] = "draft updated"
        return result

    def get_draft(self, draft_id: str) -> dict:
        result = self._run_tool(
            "GMAIL_GET_DRAFT",
            {"draft_id": draft_id, "format": "full"},
        )
        if not result.get("ok"):
            return result
        raw = result.get("raw") or {}
        fields = _extract_draft_fields(raw)
        if _should_retry_raw(raw, fields):
            raw_result = self._run_tool(
                "GMAIL_GET_DRAFT",
                {"draft_id": draft_id, "format": "raw"},
            )
            if raw_result.get("ok"):
                extra = _extract_draft_fields(raw_result.get("raw") or {})
                for key in ("recipient", "subject", "body"):
                    val = extra.get(key)
                    if not val:
                        continue
                    if key == "body" or not fields.get(key):
                        fields[key] = val
        result["draft_id"] = draft_id
        result["resolved_params"] = {
            k: v for k, v in fields.items() if k in ("recipient", "subject", "body") and v
        }
        result["detail"] = "draft loaded"
        return result

    def delete_draft(self, draft_id: str) -> dict:
        result = self._run_tool("GMAIL_DELETE_DRAFT", {"draft_id": draft_id})
        if result.get("ok"):
            result["detail"] = "draft deleted"
        return result

    def send_draft(self, draft_id: str) -> dict:
        result = self._run_tool("GMAIL_SEND_DRAFT", {"draft_id": draft_id})
        if result.get("ok"):
            result["detail"] = "email sent"
        return result

    def list_drafts(self, *, limit: int = LIST_RECENT_LIMIT) -> dict:
        cap = max(1, min(int(limit or LIST_RECENT_LIMIT), LIST_RECENT_LIMIT))
        # GMAIL_LIST_DRAFTS: verbose + max_results. One page only — no page_token.
        result = self._run_tool(
            "GMAIL_LIST_DRAFTS",
            {"verbose": True, "max_results": cap},
        )
        items = _list_draft_items(result.get("raw") or result)
        result["drafts"] = items if result.get("ok") else []
        if result.get("ok"):
            result["detail"] = "drafts listed"
        return result


_EXECUTORS = {
    "native": NativeExecutor(),
    "composio": ComposioExecutor(),
}


@dataclass
class Draft:
    provider: str
    remote_id: str | None
    recipient: str = ""
    subject: str = ""
    body: str = ""
    updated_at: str | None = None
    action_id: str | None = None

    def to_dict(self) -> dict:
        out = {
            "provider": self.provider,
            "remote_id": self.remote_id,
            "recipient": self.recipient,
            "subject": self.subject,
            "body": self.body,
            "updated_at": self.updated_at,
            "action_id": self.action_id,
            "body_snippet": (self.body or "")[:160],
        }
        if self.provider == "gmail":
            out["gmail_draft_id"] = self.remote_id
        return out


class MailProvider(Protocol):
    name: str

    def list_recent(
        self, limit: int = LIST_RECENT_LIMIT, skip_remote_id: str | None = None
    ) -> list[dict]:
        ...

    def get(self, remote_id: str) -> dict:
        ...

    def create(self, params: dict) -> dict:
        ...

    def update(self, remote_id: str, params: dict) -> dict:
        ...

    def send(self, remote_id: str) -> dict:
        ...

    def delete(self, remote_id: str) -> dict:
        ...


def _list_draft_items(raw) -> list:
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if not isinstance(raw, dict):
        return []
    if isinstance(raw.get("drafts"), list):
        return [item for item in raw["drafts"] if isinstance(item, dict)]
    data = _as_dict(raw.get("data"))
    for key in ("drafts", "items"):
        val = data.get(key)
        if isinstance(val, list):
            return [item for item in val if isinstance(item, dict)]
    nested = _as_dict(data.get("data"))
    if isinstance(nested.get("drafts"), list):
        return [item for item in nested["drafts"] if isinstance(item, dict)]
    return []


def _extract_updated_at(item: dict) -> str | None:
    if not isinstance(item, dict):
        return None
    data = _as_dict(item.get("data"))
    msg = item.get("message") if isinstance(item.get("message"), dict) else None
    if msg is None and isinstance(data.get("message"), dict):
        msg = data["message"]
    payload = msg.get("payload") if isinstance(msg, dict) and isinstance(msg.get("payload"), dict) else None
    for src in (item, data, msg or {}, payload or {}):
        if not isinstance(src, dict):
            continue
        for key in (
            "updated_at",
            "updatedAt",
            "internalDate",
            "internal_date",
            "date",
            "time",
            "timestamp",
        ):
            val = src.get(key)
            if val:
                return str(val)
    headers = _header_map((payload or {}).get("headers"))
    if headers.get("date"):
        return headers["date"]
    return None


def _draft_from_list_item(item: dict, *, provider: str) -> dict | None:
    if not isinstance(item, dict):
        return None
    candidates = [item, {"data": item}]
    message = item.get("message")
    if isinstance(message, dict):
        candidates.append(message)
        candidates.append({"data": message})
    inner = _as_dict(item.get("data"))
    if inner:
        candidates.append({"data": inner})
        if isinstance(inner.get("message"), dict):
            candidates.append({"data": inner["message"]})
    remote_id = None
    fields: dict = {}
    for cand in candidates:
        remote_id = remote_id or _extract_draft_id(cand)
        for key, val in _extract_draft_fields(cand).items():
            fields.setdefault(key, val)
    if not remote_id:
        return None
    out = Draft(
        provider=provider,
        remote_id=str(remote_id),
        recipient=str(fields.get("recipient") or "").strip(),
        subject=str(fields.get("subject") or "").strip(),
        body=str(fields.get("body") or ""),
        updated_at=_extract_updated_at(item),
    ).to_dict()
    snippet = str(fields.get("snippet") or "").strip()
    if snippet:
        out["snippet"] = snippet
    return out


def _updated_at_sort_value(value) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number / 1000.0 if number > 1e12 else number
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        number = float(text)
        return number / 1000.0 if len(text) >= 13 else number
    try:
        from datetime import datetime

        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _sort_newest_first(drafts: list[dict]) -> list[dict]:
    if not any(_updated_at_sort_value(d.get("updated_at")) is not None for d in drafts):
        return list(drafts)

    def key(pair):
        index, draft = pair
        ts = _updated_at_sort_value(draft.get("updated_at"))
        if ts is None:
            return (1, 0.0, index)
        return (0, -ts, index)

    return [draft for _, draft in sorted(enumerate(drafts), key=key)]


def _is_rate_limited(result: dict | None) -> bool:
    if not result or result.get("ok"):
        return False
    if result.get("error") in ("composio_not_connected", "not_connected"):
        return False
    blob = f"{result.get('error') or ''} {result.get('detail') or ''}".lower()
    return (
        "429" in blob
        or "userratelimitexceeded" in blob
        or "rate limit" in blob
        or "ratelimitexceeded" in blob
    )


class GmailProvider:
    """Gmail drafts via the existing ComposioExecutor (create/update/get/delete/send + list)."""

    name = "gmail"

    def __init__(self, executor=None):
        self._executor = executor

    @property
    def executor(self):
        return self._executor or _EXECUTORS.get("composio")

    def list_recent(
        self, limit: int = LIST_RECENT_LIMIT, skip_remote_id: str | None = None
    ) -> list[dict]:
        fn = getattr(self.executor, "list_drafts", None)
        if not fn:
            return []
        cap = max(1, min(int(limit or LIST_RECENT_LIMIT), LIST_RECENT_LIMIT))
        result = fn(limit=cap)
        if _is_rate_limited(result):
            time.sleep(LIST_DRAFTS_BACKOFF_S)
            result = fn(limit=cap)
            if _is_rate_limited(result) or not result.get("ok"):
                return []
        if not result.get("ok"):
            return []
        items = result.get("drafts") or _list_draft_items(result.get("raw") or {})
        drafts = []
        for item in items:
            draft = _draft_from_list_item(item, provider="gmail")
            if not draft or not draft.get("remote_id"):
                continue
            if skip_remote_id and draft["remote_id"] == skip_remote_id:
                continue
            drafts.append(draft)
        return _sort_newest_first(drafts)[:cap]

    def get(self, remote_id: str) -> dict:
        fn = getattr(self.executor, "get_draft", None)
        if not fn:
            return {"ok": False, "error": "unsupported", "detail": "no get_draft"}
        return fn(remote_id)

    def create(self, params: dict) -> dict:
        fn = getattr(self.executor, "create_draft", None)
        if not fn:
            return {"ok": False, "error": "unsupported", "detail": "no create_draft"}
        return fn(params)

    def update(self, remote_id: str, params: dict) -> dict:
        fn = getattr(self.executor, "update_draft", None)
        if not fn:
            return {"ok": False, "error": "unsupported", "detail": "no update_draft"}
        return fn(remote_id, params)

    def send(self, remote_id: str) -> dict:
        fn = getattr(self.executor, "send_draft", None)
        if not fn:
            return {"ok": False, "error": "unsupported", "detail": "no send_draft"}
        return fn(remote_id)

    def delete(self, remote_id: str) -> dict:
        fn = getattr(self.executor, "delete_draft", None)
        if not fn:
            return {"ok": False, "error": "unsupported", "detail": "no delete_draft"}
        return fn(remote_id)


class OutlookProvider:
    """Outlook drafts stub.

    Do not invent Composio Outlook tool slugs. Discover them at runtime
    (toolkit search / tools.get) when this provider is wired up.
    """

    name = "outlook"

    def list_recent(
        self, limit: int = LIST_RECENT_LIMIT, skip_remote_id: str | None = None
    ) -> list[dict]:
        return []

    def get(self, remote_id: str) -> dict:
        return _outlook_not_connected()

    def create(self, params: dict) -> dict:
        return _outlook_not_connected()

    def update(self, remote_id: str, params: dict) -> dict:
        return _outlook_not_connected()

    def send(self, remote_id: str) -> dict:
        return _outlook_not_connected()

    def delete(self, remote_id: str) -> dict:
        return _outlook_not_connected()


def _outlook_not_connected() -> dict:
    return {
        "ok": False,
        "error": "not_connected",
        "detail": "Outlook is not connected",
    }


def active_mail_providers() -> list:
    return [GmailProvider()]


def _mail_provider(name: str | None):
    key = (name or "gmail").strip().lower()
    if key == "gmail":
        return GmailProvider()
    if key == "outlook":
        return OutlookProvider()
    return None


def _action_remote_id(action: dict | None) -> str | None:
    if not action:
        return None
    return action.get("mail_draft_id") or action.get("gmail_draft_id")


def _local_unsynced_drafts(
    user_id: str, conversation_id: str, *, open_action: dict | None
) -> list[dict]:
    """Crash-resumable send_email rows in this chat that never got a remote id."""
    open_id = (open_action or {}).get("action_id")
    out = []
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM actions_log
            WHERE user_id = ? AND conversation_id = ?
              AND action_type = 'send_email'
            ORDER BY updated_at DESC
            """,
            (user_id, conversation_id),
        ).fetchall()
    for row in rows:
        action = _row_to_action(row)
        if action.get("action_id") == open_id:
            continue
        if _action_remote_id(action):
            continue
        if action["status"] != "CANCELLED" or action.get("gmail_draft_kept"):
            continue
        params = action.get("resolved_params") or {}
        recipient = str(params.get("recipient") or "").strip()
        subject = str(params.get("subject") or "").strip()
        body = str(params.get("body") or "")
        if not (recipient or subject.strip() or body.strip()):
            continue
        updated_at = None
        try:
            updated_at = row["updated_at"]
        except (KeyError, IndexError):
            pass
        out.append(
            Draft(
                provider=action.get("mail_provider") or "gmail",
                remote_id=None,
                recipient=recipient,
                subject=subject,
                body=body,
                updated_at=updated_at,
                action_id=action["action_id"],
            ).to_dict()
        )
    return out


def list_recent_drafts(user_id: str, conversation_id: str) -> list[dict]:
    """Mailbox recent drafts (~50, one page) plus local unsynced rows (no remote id).

    Dedupes by (provider, remote_id). Skips the draft already on the open composer.
    """
    open_action = get_open_action(user_id, conversation_id)
    skip_remote = _action_remote_id(open_action)
    merged: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for provider in active_mail_providers():
        batch = provider.list_recent(
            limit=LIST_RECENT_LIMIT, skip_remote_id=skip_remote
        )
        for draft in batch:
            remote = draft.get("remote_id")
            if not remote:
                continue
            if skip_remote and remote == skip_remote:
                continue
            key = (str(draft.get("provider") or "gmail"), str(remote))
            if key in seen:
                continue
            seen.add(key)
            merged.append(draft)
    for draft in _local_unsynced_drafts(
        user_id, conversation_id, open_action=open_action
    ):
        remote = draft.get("remote_id")
        if remote:
            key = (str(draft.get("provider") or "gmail"), str(remote))
            if key in seen:
                continue
            seen.add(key)
        merged.append(draft)
    return _sort_newest_first(merged)


def list_stashable_gmail_drafts(user_id: str, conversation_id: str) -> list[dict]:
    """Alias — resume uses list_recent_drafts."""
    return list_recent_drafts(user_id, conversation_id)


def load_draft(provider: str | None, remote_id: str | None) -> dict:
    if not remote_id:
        return {"ok": False, "error": "no_id", "detail": "no remote id"}
    impl = _mail_provider(provider)
    if impl is None:
        return {
            "ok": False,
            "error": "unsupported",
            "detail": f"unknown mail provider {provider}",
        }
    return impl.get(remote_id)


def execute_action(action_type: str, params: dict) -> dict:
    """Dispatch to the executor named in the whitelist entry — the only path
    from an action_type to a side effect."""
    entry = ACTION_WHITELIST.get(action_type)
    if entry is None:
        return {"ok": False, "detail": "not a whitelisted action", "error": "unsupported"}
    return _EXECUTORS[entry["executor"]].execute(action_type, params)


def upsert_gmail_draft(
    action: dict | None,
    params: dict | None = None,
    *,
    include_attachments: bool = False,
) -> tuple[dict | None, dict]:
    """Create or update the Gmail draft for an open send_email row.

    No-ops when Composio is disconnected or the fields are not ready. Never
    sends mail.
    """
    if action is None:
        return None, {"ok": False, "error": "not_open", "detail": "no action"}
    params = dict(params if params is not None else (action.get("resolved_params") or {}))
    if not include_attachments:
        params.pop("attachments", None)
    if not gmail_draft_ready(params):
        return action, {
            "ok": False,
            "error": "not_ready",
            "detail": "need a To address and subject or body",
        }
    executor = _EXECUTORS.get("composio")
    draft_id = action.get("gmail_draft_id")
    if draft_id:
        update = getattr(executor, "update_draft", None)
        if not update:
            return action, {"ok": False, "error": "unsupported", "detail": "no update_draft"}
        result = update(draft_id, params)
        return action, result
    create = getattr(executor, "create_draft", None)
    if not create:
        return action, {"ok": False, "error": "unsupported", "detail": "no create_draft"}
    result = create(params)
    new_id = result.get("draft_id")
    if result.get("ok") and new_id:
        updated = update_action(action["action_id"], gmail_draft_id=new_id)
        return updated or action, result
    return action, result


def delete_gmail_draft(action: dict | None) -> dict:
    if not action:
        return {"ok": True, "error": None, "detail": "nothing"}
    draft_id = action.get("gmail_draft_id")
    if not draft_id:
        return {"ok": True, "error": None, "detail": "no draft"}
    executor = _EXECUTORS.get("composio")
    fn = getattr(executor, "delete_draft", None)
    if not fn:
        return {"ok": True, "error": None, "detail": "no delete_draft"}
    return fn(draft_id)


def load_gmail_draft(draft_id: str) -> dict:
    return load_draft("gmail", draft_id)


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
    params = action["resolved_params"]
    draft_id = action.get("gmail_draft_id")
    executor = _EXECUTORS.get(ACTION_WHITELIST.get(action["action_type"], {}).get("executor"))
    if (
        action["action_type"] == "send_email"
        and draft_id
        and executor is not None
        and getattr(executor, "send_draft", None)
    ):
        update = getattr(executor, "update_draft", None)
        if update:
            refreshed = update(draft_id, params)
            if not refreshed.get("ok"):
                update_action(
                    action_id, status="FAILED", error=refreshed.get("error") or "execution_failed"
                )
                return "FAILED", refreshed
        result = executor.send_draft(draft_id)
    else:
        result = execute_action(action["action_type"], params)
    if result["ok"]:
        update_action(action_id, status="SUCCESS")
        if draft_id:
            consume_gmail_draft(draft_id)
        return "SUCCESS", result
    update_action(action_id, status="FAILED", error=result.get("error") or "execution_failed")
    return "FAILED", result
