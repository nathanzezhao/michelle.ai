"""R1 — ACTION engine acceptance suite (SPEC-PIPELINE §13, criteria 1–4, 6–15).

Criterion 5 is R0 itself (the pre-existing suites in this directory) and is
deliberately not duplicated here.

Runs fully deterministic: LLM_PROVIDER=mock + INTENT_MODE=rules (conftest),
COMPOSIO_API_KEY unset, and subprocess.run monkeypatched so `open -a` can
never launch a real macOS app from the test run.

Coverage map (§13 criterion → tests):
  1  → test_engine_and_no_task_fields_* (chat/retrieve/remember/blank),
       test_open_app_success (action)
  2  → test_open_app_success, test_open_app_unknown_app_fails_honestly
  3  → test_email_complete_params_goes_pending
  4  → test_non_whitelisted_order_is_chat_no_row (delete files / book flight),
       test_unsupported_after_classify_no_row,
       test_create_action_rejects_non_whitelisted,
       test_execute_action_unsupported_never_dispatches
  6  → test_email_missing_params_awaiting_then_pending_same_task
  7  → test_confirm_executes_pending_email, test_cancel_never_executes
  8  → test_confirm_unknown_task_graceful, test_confirm_terminal_task_graceful,
       test_confirm_mismatched_user_graceful
  9  → test_terminal_rows_never_mutated
  10 → test_typed_yes_never_confirms_action,
       test_yes_with_action_and_memory_both_pending
  11 → test_awaiting_input_unrelated_message_drops,
       test_pending_unrelated_message_drops
  12 → test_new_action_replaces_pending_action,
       test_new_action_replaces_awaiting_action
  13 → test_no_composio_key_confirm_fails_gracefully
  14 → test_startup_sweep_closes_open_rows_and_executes_nothing
  15 → audit asserts folded into criterion-2 tests (row + visible sentence)
       and criterion-4 tests (near-miss leaves no row)
"""

import base64
import io
import json
import sqlite3
import types
import wave
from uuid import uuid4

import pytest

import actions
import intent
import long_term_memory
import main
import memory
import whisper
from conftest import chat

_REAL_TRANSCRIBE = whisper.transcribe_wav

GENERIC_ERROR = (
    "Sorry, I am having some trouble with this right now. Please try again later."
)

TASK_FIELDS = (
    "task_id",
    "task_status",
    "action_type",
    "risk",
    "confirm_required",
    "missing_params",
    "resolved_params",
)

EMAIL_FULL = "send an email to alex@example.com subject hi body hello"
EMAIL_PARTIAL = "send an email to alex"
EMAIL_PARAMS = {"recipient": "alex@example.com", "subject": "hi", "body": "hello"}


# --- DB helpers (direct actions_log reads on the conftest temp DB) -----------


def _db(sql, params=()):
    with sqlite3.connect(actions.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def action_rows(ids):
    return _db(
        "SELECT * FROM actions_log WHERE user_id = ? AND conversation_id = ?",
        (ids["user_id"], ids["conversation_id"]),
    )


def open_rows(ids):
    return [
        r for r in action_rows(ids) if r["status"] in actions.NON_TERMINAL_STATUSES
    ]


def fetch_row(action_id):
    rows = _db("SELECT * FROM actions_log WHERE action_id = ?", (action_id,))
    assert len(rows) == 1
    return rows[0]


def payload(row):
    return json.loads(row["payload_json"])


def native_action_calls(fake):
    """open -a and close/quit osascript — not the running-process listing."""
    out = []
    for argv in fake.calls:
        if not argv:
            continue
        if argv[:2] == ["open", "-a"]:
            out.append(argv)
        elif argv[:2] == ["osascript", "-e"] and "every process" not in argv[2]:
            out.append(argv)
    return out


def assert_no_task_fields(body):
    for field in TASK_FIELDS:
        assert field not in body, f"non-action turn leaked task field {field!r}"


# --- Fakes -------------------------------------------------------------------


class FakeSubprocess:
    """Stand-in for subprocess.run: records argv, never touches macOS."""

    def __init__(self):
        self.calls = []
        self.returncode = 0
        self.running = ["Notes", "Safari", "Google Chrome", "Slack", "Mail", "Finder"]

    def run(self, argv, **kwargs):
        # NativeExecutor must pass args as a list, never a shell string (§5).
        assert isinstance(argv, list)
        assert not kwargs.get("shell")
        self.calls.append(argv)
        is_list = (
            argv
            and argv[0] == "osascript"
            and any("every process" in str(part) for part in argv)
        )
        if is_list:
            return types.SimpleNamespace(
                returncode=0, stdout=", ".join(self.running), stderr=""
            )
        return types.SimpleNamespace(
            returncode=self.returncode, stdout="", stderr=""
        )


class FakeComposio:
    """Recording composio executor; result configurable per test."""

    def __init__(self, result=None):
        self.calls = []
        self.draft_calls = []
        self.drafts = {}
        self._n = 0
        self._clock = 0
        self.result = result or {"ok": True, "detail": "email sent", "error": None}
        self.connected = True

    def _next_id(self):
        self._n += 1
        return f"r-fake-{self._n}"

    def execute(self, action_type, params):
        self.calls.append((action_type, dict(params)))
        return self.result

    def create_draft(self, params):
        self.draft_calls.append(("create", dict(params)))
        if not self.connected:
            return {
                "ok": False,
                "error": "composio_not_connected",
                "detail": "COMPOSIO_API_KEY is not set",
            }
        draft_id = self._next_id()
        stored = dict(params)
        self._clock += 1
        stored["updated_at"] = str(self._clock)
        self.drafts[draft_id] = stored
        return {"ok": True, "draft_id": draft_id, "error": None, "detail": "draft created"}

    def update_draft(self, draft_id, params):
        self.draft_calls.append(("update", draft_id, dict(params)))
        if not self.connected:
            return {
                "ok": False,
                "error": "composio_not_connected",
                "detail": "COMPOSIO_API_KEY is not set",
            }
        if draft_id not in self.drafts:
            return {"ok": False, "error": "composio_error", "detail": "unknown draft"}
        stored = dict(params)
        self._clock += 1
        stored["updated_at"] = str(self._clock)
        self.drafts[draft_id] = stored
        return {"ok": True, "draft_id": draft_id, "error": None, "detail": "draft updated"}

    def get_draft(self, draft_id):
        self.draft_calls.append(("get", draft_id))
        if draft_id not in self.drafts:
            return {"ok": False, "error": "composio_error", "detail": "not found"}
        stored = self.drafts[draft_id]
        payload = stored["get"] if isinstance(stored.get("get"), dict) else stored
        fields = {}
        for src in (payload, {"data": payload}):
            for key, val in actions._extract_draft_fields(src).items():
                if key in ("recipient", "subject", "body") and val and key not in fields:
                    fields[key] = val
        for key in ("recipient", "subject", "body"):
            val = payload.get(key) if isinstance(payload, dict) else None
            if key not in fields and isinstance(val, str) and val.strip():
                fields[key] = val.strip()
        return {
            "ok": True,
            "draft_id": draft_id,
            "resolved_params": fields,
            "error": None,
        }

    def delete_draft(self, draft_id):
        self.draft_calls.append(("delete", draft_id))
        self.drafts.pop(draft_id, None)
        return {"ok": True, "error": None, "detail": "draft deleted"}

    def send_draft(self, draft_id):
        self.draft_calls.append(("send", draft_id))
        if draft_id not in self.drafts:
            return {"ok": False, "error": "composio_error", "detail": "unknown draft"}
        self.drafts.pop(draft_id, None)
        return {"ok": True, "detail": "email sent", "error": None}

    def list_drafts(self, *, limit=50):
        self.draft_calls.append(("list", limit))
        if not self.connected:
            return {
                "ok": False,
                "error": "composio_not_connected",
                "detail": "COMPOSIO_API_KEY is not set",
                "drafts": [],
            }
        items = []
        for draft_id, params in self.drafts.items():
            data = dict(params)
            item = {"id": draft_id, "draft_id": draft_id, "data": data}
            if params.get("updated_at"):
                item["updated_at"] = params["updated_at"]
            items.append(item)
        return {"ok": True, "drafts": items[:limit], "error": None}


class BoomExecutor:
    """Fails the test if anything dispatches to it."""

    def __init__(self, label):
        self.label = label

    def execute(self, *args, **kwargs):
        raise AssertionError(f"{self.label} executor must not be called")


@pytest.fixture(autouse=True)
def _no_real_side_effects(monkeypatch):
    """Safety net for every test in this module: `open -a` can never reach the
    real subprocess (a stray route would silently FAIL the action instead of
    opening an app), and COMPOSIO_API_KEY stays unset."""

    def _blocked(*args, **kwargs):
        raise AssertionError("real subprocess.run reached during R1 tests")

    monkeypatch.setattr(actions.subprocess, "run", _blocked)
    monkeypatch.delenv("COMPOSIO_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def _no_real_whisper(monkeypatch):
    """CI must never download a Whisper model. JSON tests don't call
    transcribe_wav; audio tests replace this with an explicit fake."""

    def _blocked(*args, **kwargs):
        raise AssertionError("real whisper.transcribe_wav reached during tests")

    monkeypatch.setattr(whisper, "transcribe_wav", _blocked)


@pytest.fixture
def fake_open(monkeypatch):
    fake = FakeSubprocess()
    monkeypatch.setattr(actions.subprocess, "run", fake.run)
    monkeypatch.setattr(actions, "_list_installed_app_names", lambda: [])
    return fake


@pytest.fixture
def fake_composio(monkeypatch):
    fake = FakeComposio()
    monkeypatch.setitem(actions._EXECUTORS, "composio", fake)
    return fake


def make_pending_email(client, ids):
    body = chat(client, EMAIL_FULL, ids)
    assert body["task_status"] == "PENDING"
    return body


def confirm(client, ids, task_id, decision):
    resp = client.post(
        "/action/confirm",
        json={
            "task_id": task_id,
            "decision": decision,
            "conversation_id": ids["conversation_id"],
            "user_id": ids["user_id"],
        },
    )
    assert resp.status_code == 200
    return resp.json()


def seed_action_with_status(user_id, conversation_id, status):
    """Insert an email action row and force it into the given status directly
    (bypasses the state machine on purpose — seeding, not testing)."""
    row = actions.create_action(
        user_id,
        conversation_id,
        "send_email",
        dict(EMAIL_PARAMS),
        [],
        "PENDING",
    )
    with sqlite3.connect(actions.DB_PATH) as conn:
        conn.execute(
            "UPDATE actions_log SET status = ? WHERE action_id = ?",
            (status, row["action_id"]),
        )
    return row["action_id"]


# --- Criterion 1: engine on every turn, task fields only on action turns -----


def test_engine_and_no_task_fields_chat_turn(client, ids):
    body = chat(client, "hey", ids)
    assert body["engine"] == "chat"
    assert_no_task_fields(body)


def test_engine_and_no_task_fields_retrieve_turn(client, ids):
    body = chat(client, "what's our refund policy?", ids)
    assert body["engine"] == "retrieve"
    assert_no_task_fields(body)


def test_engine_and_no_task_fields_remember_turn(client, ids):
    body = chat(client, "keep in mind my age is 22", ids)
    assert body["engine"] == "remember"
    assert body["remembered"] == [
        {"key": "age", "value": "22", "priority": "high"}
    ]
    assert_no_task_fields(body)


def test_engine_and_no_task_fields_blank_message_guardrail(client, ids):
    body = chat(client, "", ids)
    assert body["answer"] == "Please type a message first."
    assert body["engine"] == "chat"
    assert_no_task_fields(body)


# --- Criterion 2 (+15 audit): open_app low-risk auto-execute ------------------


def test_open_app_success(client, ids, fake_open):
    body = chat(client, "open Notes", ids)
    assert body["engine"] == "action"
    assert body["action_type"] == "open_app"
    assert body["risk"] == "low"
    assert body["confirm_required"] is False
    assert body["task_status"] == "SUCCESS"
    assert body["missing_params"] == []
    # Executor got exactly one list-args invocation.
    assert fake_open.calls == [["open", "-a", "Notes"]]
    # §13-15 audit: an actions_log row AND a user-visible sentence, always.
    rows = action_rows(ids)
    assert len(rows) == 1
    assert rows[0]["action_id"] == body["task_id"]
    assert rows[0]["status"] == "SUCCESS"
    assert "Opened Notes" in body["answer"]


def test_open_multiple_apps_one_action(client, ids, fake_open):
    body = chat(client, "open Notes and Safari", ids)
    assert body["engine"] == "action"
    assert body["action_type"] == "open_app"
    assert body["confirm_required"] is False
    assert body["task_status"] == "SUCCESS"
    assert body["resolved_params"]["app_names"] == ["Notes", "Safari"]
    assert fake_open.calls == [["open", "-a", "Notes"], ["open", "-a", "Safari"]]
    assert "Opened Notes and Safari" in body["answer"]


def test_open_oxford_list(client, ids, fake_open):
    body = chat(client, "open Chrome, Slack, and Notes", ids)
    assert body["action_type"] == "open_app"
    assert body["task_status"] == "SUCCESS"
    assert body["resolved_params"]["app_names"] == ["Chrome", "Slack", "Notes"]
    assert fake_open.calls == [
        ["open", "-a", "Chrome"],
        ["open", "-a", "Slack"],
        ["open", "-a", "Notes"],
    ]


def test_open_app_unknown_app_fails_honestly(client, ids, fake_open):
    fake_open.returncode = 1
    body = chat(client, "open Zorbulon", ids)
    assert body["engine"] == "action"
    assert body["task_status"] == "FAILED"
    assert "couldn't find an app called Zorbulon" in body["answer"]
    rows = action_rows(ids)
    assert len(rows) == 1
    assert rows[0]["status"] == "FAILED"
    assert payload(rows[0])["error"] == "app_not_found"


@pytest.mark.parametrize(
    "text,app_name",
    [
        ("yo pull up chrome", "chrome"),
        ("fire up vs code rq", "vs code"),
        ("hop into discord", "discord"),
        ("pls launch xyzzyqorp", "xyzzyqorp"),
        ("open Notes rq", "Notes"),
        ("can u open Notes+", "Notes+"),
        ("bruh bring up Calculator", "Calculator"),
    ],
)
def test_open_app_slang_and_gibberish_names(client, ids, fake_open, text, app_name):
    """App names are not a closed list. Slang verbs still route to ACTION."""
    body = chat(client, text, ids)
    assert body["engine"] == "action"
    assert body["action_type"] == "open_app"
    assert body["task_status"] == "SUCCESS"
    assert fake_open.calls == [["open", "-a", app_name]]


def test_now_close_them_does_not_chat_the_crumb(client, ids, fake_open):
    opened = chat(client, "open Notes and Safari", ids)
    assert opened["task_status"] == "SUCCESS"
    body = chat(client, "now close them", ids)
    assert body["engine"] == "action"
    assert body["action_type"] == "close_app"
    assert body["task_status"] == "SUCCESS"
    assert body["resolved_params"]["app_names"] == ["Notes", "Safari"]
    assert "Closed Notes and Safari" in body["answer"]
    assert "[mock mode]" not in body["answer"]
    assert "I heard:" not in body["answer"]


def test_chat_then_open_runs_action_and_replies(client, ids, fake_open):
    body = chat(client, "hey how are you, open Notes", ids)
    assert body["engine"] == "action"
    assert body["action_type"] == "open_app"
    assert body["task_status"] == "SUCCESS"
    assert "Opened Notes" in body["answer"]
    assert fake_open.calls == [["open", "-a", "Notes"]]


def test_open_then_email_does_both_and_confirms(client, ids, fake_open, fake_composio):
    body = chat(
        client,
        "open Notes then send an email to alex@example.com subject hi body hello",
        ids,
    )
    assert body["engine"] == "action"
    assert body["action_type"] == "send_email"
    assert body["task_status"] == "PENDING"
    assert body["confirm_required"] is True
    assert "Opened Notes" in body["answer"]
    assert "alex@example.com" in body["answer"]
    assert fake_open.calls == [["open", "-a", "Notes"]]
    out = confirm(client, ids, body["task_id"], "confirm")
    assert out["task_status"] == "SUCCESS"
    assert out["confirm_required"] is False
    assert "Sent the email to alex@example.com" in out["answer"]


def test_two_emails_queue_second_after_confirm(client, ids, fake_composio):
    body = chat(
        client,
        "send an email to alex@example.com subject one body aaa then "
        "send an email to sam@example.com subject two body bbb",
        ids,
    )
    assert body["task_status"] == "PENDING"
    assert body["action_type"] == "send_email"
    assert "alex@example.com" in body["answer"]
    rows = action_rows(ids)
    queued = payload(rows[0]).get("queue") or []
    assert len(queued) == 1
    assert queued[0]["resolved_params"]["recipient"] == "sam@example.com"
    out = confirm(client, ids, body["task_id"], "confirm")
    assert out["confirm_required"] is True
    assert out["task_status"] == "PENDING"
    assert out["task_id"] != body["task_id"]
    assert "sam@example.com" in out["answer"]


def test_open_app_then_bare_name_continues(client, ids, fake_open):
    """If the first turn misses the app, a bare name like 'Notes' must continue
    the paused action — not get dropped as a topic change (live log bug)."""
    first = chat(client, "open app", ids)
    assert first["engine"] == "action"
    assert first["task_status"] == "AWAITING_INPUT"
    second = chat(client, "Notes", ids)
    assert second["engine"] == "action"
    assert second["task_id"] == first["task_id"]
    assert second["task_status"] == "SUCCESS"
    assert fake_open.calls == [["open", "-a", "Notes"]]


def test_close_app_success(client, ids, fake_open):
    body = chat(client, "close Notes", ids)
    assert body["engine"] == "action"
    assert body["action_type"] == "close_app"
    assert body["risk"] == "low"
    assert body["confirm_required"] is False
    assert body["task_status"] == "SUCCESS"
    scripts = native_action_calls(fake_open)
    assert scripts == [["osascript", "-e", 'tell application "Notes" to close every window']]
    assert "Closed Notes" in body["answer"]


def test_close_multiple_apps_one_action(client, ids, fake_open):
    body = chat(client, "close Notes and Safari", ids)
    assert body["engine"] == "action"
    assert body["action_type"] == "close_app"
    assert body["confirm_required"] is False
    assert body["task_status"] == "SUCCESS"
    assert body["resolved_params"]["app_names"] == ["Notes", "Safari"]
    scripts = native_action_calls(fake_open)
    assert scripts == [
        ["osascript", "-e", 'tell application "Notes" to close every window'],
        ["osascript", "-e", 'tell application "Safari" to close every window'],
    ]
    assert "Closed Notes and Safari" in body["answer"]


def test_close_typo_verb_still_closes(client, ids, fake_open):
    body = chat(client, "clsoe Notes", ids)
    assert body["action_type"] == "close_app"
    assert body["task_status"] == "SUCCESS"
    assert native_action_calls(fake_open) == [
        ["osascript", "-e", 'tell application "Notes" to close every window']
    ]


def test_close_typo_name_fuzzy_matches(client, ids, fake_open):
    body = chat(client, "close Safar", ids)
    assert body["action_type"] == "close_app"
    assert body["task_status"] == "SUCCESS"
    assert native_action_calls(fake_open) == [
        ["osascript", "-e", 'tell application "Safari" to close every window']
    ]
    assert "Closed Safari" in body["answer"]


def test_close_unknown_app_fails_honestly(client, ids, fake_open):
    body = chat(client, "close Zorbulon", ids)
    assert body["engine"] == "action"
    assert body["action_type"] == "close_app"
    assert body["task_status"] == "FAILED"
    assert "couldn't find an app called Zorbulon" in body["answer"]
    assert native_action_calls(fake_open) == []


def test_quit_app_goes_pending(client, ids, fake_open):
    body = chat(client, "quit Safari", ids)
    assert body["engine"] == "action"
    assert body["action_type"] == "quit_app"
    assert body["risk"] == "high"
    assert body["confirm_required"] is True
    assert body["task_status"] == "PENDING"
    assert "Ready to quit Safari" in body["answer"]
    assert native_action_calls(fake_open) == []


def test_quit_typo_verb_still_quits_after_confirm(client, ids, fake_open):
    body = chat(client, "quti Safari", ids)
    assert body["action_type"] == "quit_app"
    assert body["confirm_required"] is True
    out = confirm(client, ids, body["task_id"], "confirm")
    assert out["task_status"] == "SUCCESS"
    assert native_action_calls(fake_open) == [
        ["osascript", "-e", 'tell application "Safari" to quit']
    ]
    assert "Quit Safari" in out["answer"]


def test_quit_multiple_one_confirm(client, ids, fake_open):
    body = chat(client, "quit Chrome, Slack, and Notes", ids)
    assert body["action_type"] == "quit_app"
    assert body["confirm_required"] is True
    assert body["resolved_params"]["app_names"] == ["Chrome", "Slack", "Notes"]
    assert "Quit them?" in body["answer"]
    assert native_action_calls(fake_open) == []
    out = confirm(client, ids, body["task_id"], "confirm")
    assert out["task_status"] == "SUCCESS"
    assert native_action_calls(fake_open) == [
        ["osascript", "-e", 'tell application "Google Chrome" to quit'],
        ["osascript", "-e", 'tell application "Slack" to quit'],
        ["osascript", "-e", 'tell application "Notes" to quit'],
    ]


def test_close_then_quit_runs_close_and_confirms_quit(client, ids, fake_open):
    body = chat(client, "close Notes and quit Safari", ids)
    assert body["engine"] == "action"
    assert body["action_type"] == "quit_app"
    assert body["confirm_required"] is True
    assert "Closed Notes" in body["answer"]
    assert "Ready to quit Safari" in body["answer"]
    assert native_action_calls(fake_open) == [
        ["osascript", "-e", 'tell application "Notes" to close every window']
    ]
    out = confirm(client, ids, body["task_id"], "confirm")
    assert out["task_status"] == "SUCCESS"
    assert native_action_calls(fake_open) == [
        ["osascript", "-e", 'tell application "Notes" to close every window'],
        ["osascript", "-e", 'tell application "Safari" to quit'],
    ]


def test_close_the_draft_is_resume_not_close_app(client, ids, fake_composio):
    body = chat(client, "close the draft", ids)
    assert body["engine"] == "action"
    assert body["answer"] == main.RESUME_MISS_REPLY
    assert body.get("task_id") is None
    rows = [r for r in action_rows(ids) if r["action_type"] == "close_app"]
    assert rows == []


def test_quit_already_quit_does_not_hit_notes_plus(client, ids, fake_open, monkeypatch):
    fake_open.running = ["Notes+", "Safari"]
    monkeypatch.setattr(
        actions, "_list_installed_app_names", lambda: ["Notes", "Notes+", "Safari"]
    )
    body = chat(client, "quit Notes", ids)
    assert body["action_type"] == "quit_app"
    assert body["confirm_required"] is True
    out = confirm(client, ids, body["task_id"], "confirm")
    assert out["task_status"] == "SUCCESS"
    assert "already quit" in out["answer"].lower()
    assert "Notes+" not in out["answer"]
    assert native_action_calls(fake_open) == []


def test_close_already_closed_is_not_unknown(client, ids, fake_open, monkeypatch):
    fake_open.running = ["Safari"]
    monkeypatch.setattr(
        actions, "_list_installed_app_names", lambda: ["Notes", "Safari"]
    )
    body = chat(client, "close Notes", ids)
    assert body["action_type"] == "close_app"
    assert body["task_status"] == "SUCCESS"
    assert "isn't open" in body["answer"].lower()
    assert native_action_calls(fake_open) == []


def test_open_and_quit_keeps_quit_target(client, ids, fake_open):
    """Live log: 'open music and quit messages' dropped app_names on quit."""
    body = chat(client, "open Notes and quit Safari", ids)
    assert body["engine"] == "action"
    assert body["action_type"] == "quit_app"
    assert body["confirm_required"] is True
    assert body["resolved_params"]["app_names"] == ["Safari"]
    assert "Opened Notes" in body["answer"]
    assert "Ready to quit Safari" in body["answer"]
    assert native_action_calls(fake_open) == [["open", "-a", "Notes"]]
    out = confirm(client, ids, body["task_id"], "confirm")
    assert out["task_status"] == "SUCCESS"
    assert native_action_calls(fake_open) == [
        ["open", "-a", "Notes"],
        ["osascript", "-e", 'tell application "Safari" to quit'],
    ]


def test_quit_missing_name_then_bare_name_is_not_close(client, ids, fake_open, monkeypatch):
    """Live log: after quit lost the name, 'messages' ran as close_app."""
    first = chat(client, "quit app", ids)
    assert first["action_type"] == "quit_app"
    assert first["task_status"] == "AWAITING_INPUT"
    monkeypatch.setenv("INTENT_MODE", "llm")
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setattr(
        intent,
        "_classify_with_llm",
        lambda *a, **k: {
            "intent": "ACTION",
            "confidence": 0.9,
            "is_question": False,
            "kind": "ACTION",
            "memory_score": 0.0,
            "docs_score": 0.0,
            "chat_score": 0.0,
        },
    )
    monkeypatch.setattr(
        intent,
        "_analyze_action_with_llm",
        lambda *a, **k: {
            "action_type": "close_app",
            "resolved_params": {"app_names": ["messages"]},
            "missing_params": [],
            "related": False,
            "confidence": 0.9,
        },
    )
    second = chat(client, "messages", ids)
    assert second["engine"] == "action"
    assert second["action_type"] == "quit_app"
    assert second["task_id"] == first["task_id"]
    assert second["confirm_required"] is True
    assert second["resolved_params"]["app_names"] == ["messages"]
    assert native_action_calls(fake_open) == []


def test_llm_drops_app_name_but_extract_recovers(client, ids, fake_open, monkeypatch):
    """Live bug: llama returned open_app with resolved={{}} for 'open Notes'."""
    monkeypatch.setenv("INTENT_MODE", "llm")
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setattr(
        intent,
        "_classify_with_llm",
        lambda *a, **k: {
            "intent": "CHAT",
            "confidence": 0.5,
            "is_question": True,
            "kind": "CHAT",
            "memory_score": 0.0,
            "docs_score": 0.0,
            "chat_score": 0.5,
        },
    )
    monkeypatch.setattr(
        intent,
        "_analyze_action_with_llm",
        lambda *a, **k: {
            "action_type": "open_app",
            "resolved_params": {},
            "missing_params": ["app_name"],
            "related": True,
            "confidence": 0.9,
        },
    )
    body = chat(client, "open Notes", ids)
    assert body["engine"] == "action"
    assert body["task_status"] == "SUCCESS"
    assert fake_open.calls == [["open", "-a", "Notes"]]


# --- Criterion 3: complete email goes PENDING with buttons -------------------


def test_email_complete_params_goes_pending(client, ids):
    body = chat(client, EMAIL_FULL, ids)
    assert body["engine"] == "action"
    assert body["action_type"] == "send_email"
    assert body["risk"] == "high"
    assert body["task_status"] == "PENDING"
    assert body["confirm_required"] is True
    assert body["missing_params"] == []
    assert "alex@example.com" in body["answer"]
    assert action_rows(ids)[0]["status"] == "PENDING"


# --- Criterion 4 (+15): non-whitelisted orders never execute, never log ------


@pytest.mark.parametrize("text", ["delete all my files", "book a flight to tokyo"])
def test_non_whitelisted_order_is_chat_no_row(client, ids, text):
    body = chat(client, text, ids)
    assert body["engine"] == "chat"
    assert_no_task_fields(body)
    assert action_rows(ids) == []


def test_unsupported_after_classify_no_row(client, ids, monkeypatch):
    # Force the post-classify branch: intent=ACTION but the analyzer returns a
    # non-whitelisted type — code-side validation must treat it as unsupported.
    monkeypatch.setattr(
        main,
        "analyze_action_request",
        lambda *args, **kwargs: {
            "action_type": "unsupported",
            "resolved_params": {},
            "missing_params": [],
            "confidence": 0.9,
            "related": False,
        },
    )
    body = chat(client, "open Notes", ids)
    assert body["engine"] == "chat"
    assert body["answer"] == main.UNSUPPORTED_ACTION_REPLY
    assert_no_task_fields(body)
    assert action_rows(ids) == []


def test_create_action_rejects_non_whitelisted(ids):
    with pytest.raises(ValueError):
        actions.create_action(
            ids["user_id"],
            ids["conversation_id"],
            "book_flight",
            {},
            [],
            "PENDING",
        )
    assert action_rows(ids) == []


def test_execute_action_unsupported_never_dispatches(monkeypatch):
    monkeypatch.setitem(actions._EXECUTORS, "native", BoomExecutor("native"))
    monkeypatch.setitem(actions._EXECUTORS, "composio", BoomExecutor("composio"))
    result = actions.execute_action("book_flight", {})
    assert result["ok"] is False
    assert result["error"] == "unsupported"


# --- Criterion 6: AWAITING_INPUT → params supplied → PENDING, same task ------


def test_email_missing_params_awaiting_then_pending_same_task(client, ids):
    body = chat(client, EMAIL_PARTIAL, ids)
    assert body["engine"] == "action"
    assert body["action_type"] == "send_email"
    assert body["task_status"] == "AWAITING_INPUT"
    assert body["confirm_required"] is False
    assert body["missing_params"] == ["subject", "body"]
    assert "subject" in body["answer"] and "body" in body["answer"]

    follow = chat(client, "subject hi body hello", ids)
    assert follow["engine"] == "action"
    assert follow["task_id"] == body["task_id"]  # SAME task continued
    assert follow["task_status"] == "PENDING"
    assert follow["confirm_required"] is True
    assert follow["missing_params"] == []
    rows = action_rows(ids)
    assert len(rows) == 1  # continued, not replaced
    assert rows[0]["status"] == "PENDING"


def test_awaiting_email_includes_resolved_params(client, ids):
    body = chat(client, EMAIL_PARTIAL, ids)
    assert body["task_status"] == "AWAITING_INPUT"
    assert body["resolved_params"]["recipient"] == "alex"


def test_composer_style_followup_goes_pending(client, ids):
    body = chat(client, "send an email", ids)
    assert body["task_status"] == "AWAITING_INPUT"
    follow = chat(
        client, "to alex@example.com subject hi body hello", ids
    )
    assert follow["task_id"] == body["task_id"]
    assert follow["task_status"] == "PENDING"
    assert follow["resolved_params"]["recipient"] == "alex@example.com"
    assert follow["resolved_params"]["subject"] == "hi"
    assert follow["resolved_params"]["body"] == "hello"


COMPOSER_SPOKEN_BODY = (
    "Hey Dad, send an email for me and remember my name is Nathan. "
    "nevermind. sorry - email feature. And this email is being said "
    "right now because I added voice input."
)


def test_submit_draft_keeps_spoken_body_off_chat_intent(client, ids, monkeypatch):
    """Composer Send is not /chat. The body is opaque, even if it looks like
    orders, memory, or nevermind."""
    first = chat(client, "send an email", ids)
    assert first["task_status"] == "AWAITING_INPUT"

    def boom(*args, **kwargs):
        raise AssertionError("submit_draft must not run chat / memory / intent")

    monkeypatch.setattr(main, "classify_intent", boom)
    monkeypatch.setattr(main, "ask_llm", boom)
    monkeypatch.setattr(long_term_memory, "get_facts", boom)
    monkeypatch.setattr(intent, "parse_mixed_utterance", boom)

    resp = client.post(
        "/action/submit_draft",
        json={
            "recipient": "alex@example.com",
            "subject": "Project",
            "body": COMPOSER_SPOKEN_BODY,
            **ids,
        },
    )
    assert resp.status_code == 200
    out = resp.json()
    assert "dropped" not in out["answer"].lower()
    assert out.get("asked_to_save_draft") is not True
    assert out["task_id"] == first["task_id"]
    assert out["task_status"] == "PENDING"
    assert out["resolved_params"]["body"] == COMPOSER_SPOKEN_BODY
    assert len(action_rows(ids)) == 1
    hist = memory.get_history(ids["conversation_id"], limit=50)
    user_turns = [t["content"] for t in hist if t["role"] == "user"]
    assert COMPOSER_SPOKEN_BODY not in user_turns
    assert "Composer send." in user_turns


def test_submit_draft_without_open_composer_does_not_create_email(client, ids):
    resp = client.post(
        "/action/submit_draft",
        json={
            "recipient": "alex@example.com",
            "subject": "hi",
            "body": "send an email to alex@example.com subject hi body hello",
            **ids,
        },
    )
    assert resp.status_code == 200
    out = resp.json()
    assert out["task_id"] is None
    assert action_rows(ids) == []


def _silence_wav(duration_s=1.0, rate=16000):
    frames = int(rate * duration_s)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x00\x00" * frames)
    return buf.getvalue()


def test_draft_body_does_not_mutate_open_action(client, ids):
    body = chat(client, "send an email", ids)
    before = fetch_row(body["task_id"])
    resp = client.post(
        "/action/draft_body",
        json={
            "recipient": "alex@example.com",
            "subject": "hi",
            **ids,
        },
    )
    assert resp.status_code == 200
    assert isinstance(resp.json().get("body"), str)
    assert fetch_row(body["task_id"]) == before


def test_draft_body_json_transcript_fills_body_no_action_mutation(client, ids, monkeypatch):
    body = chat(client, "send an email", ids)
    before = fetch_row(body["task_id"])
    spoken = "Please let Alex know I will be about twenty minutes late tonight."
    monkeypatch.setattr(main, "polish_email_body", lambda source: "I'll be about twenty minutes late.")
    resp = client.post(
        "/action/draft_body",
        json={
            "recipient": "alex@example.com",
            "subject": "running late",
            "transcript": spoken,
            "body_from_user": True,
            **ids,
        },
    )
    assert resp.status_code == 200
    out = resp.json()
    assert out["error"] is None
    assert out["body"] == "I'll be about twenty minutes late."
    assert out["transcript"] == spoken
    assert fetch_row(body["task_id"]) == before


def test_draft_body_junk_transcript_skips_llm_and_does_not_blank(client, ids, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("polish_email_body must not run for junk transcripts")

    monkeypatch.setattr(main, "polish_email_body", boom)
    resp = client.post(
        "/action/draft_body",
        json={
            "recipient": "alex@example.com",
            "subject": "hi",
            "body": "please leave this typed draft alone",
            "transcript": "thank you",
            **ids,
        },
    )
    assert resp.status_code == 200
    out = resp.json()
    assert out["error"] == "heard_nothing"
    assert out["body"] is None
    assert out["body"] != ""
    assert out["transcript"] == "thank you"


def test_draft_body_speech_ignores_tap_leftover_when_not_from_user(client, ids, monkeypatch):
    leftover = "GENERIC TAP FLUFF about quarterly synergetic alignment"
    spoken = "Please tell Alex I will bring dessert to dinner on Friday night."
    captured = {}

    def capture(source):
        captured["prompt"] = source
        return "I'll bring dessert Friday."

    monkeypatch.setattr(main, "polish_email_body", capture)
    resp = client.post(
        "/action/draft_body",
        json={
            "recipient": "alex@example.com",
            "subject": "dinner",
            "body": leftover,
            "body_from_user": False,
            "transcript": spoken,
            **ids,
        },
    )
    assert resp.status_code == 200
    out = resp.json()
    assert out["error"] is None
    assert out["body"] == "I'll bring dessert Friday."
    prompt = captured["prompt"]
    assert spoken in prompt
    assert leftover not in prompt


def test_draft_body_multipart_audio_speech_wins_over_tap_body(client, ids, monkeypatch):
    leftover = "GENERIC TAP FLUFF leftover from generate"
    spoken = "Please tell Alex the meeting moved to Thursday afternoon this week."
    captured = {}

    monkeypatch.setattr(whisper, "transcribe_wav", lambda b: spoken)

    def capture(source):
        captured["prompt"] = source
        return "Meeting moved to Thursday afternoon."

    monkeypatch.setattr(main, "polish_email_body", capture)
    resp = client.post(
        "/action/draft_body",
        data={
            "recipient": "alex@example.com",
            "subject": "meeting",
            "body": leftover,
            "body_from_user": "false",
            **ids,
        },
        files={"audio": ("clip.wav", _silence_wav(1.0), "audio/wav")},
    )
    assert resp.status_code == 200
    out = resp.json()
    assert out["error"] is None
    assert out["body"] == "Meeting moved to Thursday afternoon."
    assert out["transcript"] == spoken
    assert leftover not in captured["prompt"]
    assert spoken in captured["prompt"]


def test_draft_body_spoken_does_not_read_long_term_facts(client, ids, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("draft_body must not read long_term_facts")

    monkeypatch.setattr(long_term_memory, "get_facts", boom)
    monkeypatch.setattr(main, "polish_email_body", lambda source: source)
    resp = client.post(
        "/action/draft_body",
        json={
            "recipient": "alex@example.com",
            "subject": "hi",
            "transcript": "Hi my name is Nathan I wanted to say thanks for lunch.",
            "body_from_user": True,
            **ids,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["error"] is None
    assert "Hi my name is Nathan" in resp.json()["body"]


def test_draft_body_does_not_call_chat_ask_llm(client, ids, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("draft_body must not use the Michelle chat LLM")

    monkeypatch.setattr(main, "ask_llm", boom)
    monkeypatch.setattr(main, "polish_email_body", lambda source: source)
    resp = client.post(
        "/action/draft_body",
        json={
            "recipient": "alex@example.com",
            "subject": "hi",
            "transcript": "Hi my name is Nathan thanks for lunch yesterday.",
            **ids,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["error"] is None


def test_draft_body_whisper_busy_does_not_call_llm(client, ids, monkeypatch):
    def busy(_wav):
        raise whisper.WhisperBusy("busy")

    def boom(*args, **kwargs):
        raise AssertionError("polish_email_body must not run when whisper is busy")

    monkeypatch.setattr(whisper, "transcribe_wav", busy)
    monkeypatch.setattr(main, "polish_email_body", boom)
    resp = client.post(
        "/action/draft_body",
        data={"recipient": "alex@example.com", "subject": "hi", **ids},
        files={"audio": ("clip.wav", _silence_wav(1.0), "audio/wav")},
    )
    assert resp.status_code == 200
    out = resp.json()
    assert out["error"] == "busy"
    assert out["body"] is None


def test_draft_body_whisper_downloading_does_not_call_llm(client, ids, monkeypatch):
    def downloading(_wav):
        raise whisper.WhisperDownloading("downloading")

    def boom(*args, **kwargs):
        raise AssertionError("polish_email_body must not run while the model downloads")

    monkeypatch.setattr(whisper, "transcribe_wav", downloading)
    monkeypatch.setattr(main, "polish_email_body", boom)
    resp = client.post(
        "/action/draft_body",
        data={"recipient": "alex@example.com", "subject": "hi", **ids},
        files={"audio": ("clip.wav", _silence_wav(1.0), "audio/wav")},
    )
    assert resp.status_code == 200
    out = resp.json()
    assert out["error"] == "downloading"
    assert out["body"] is None


def test_draft_body_short_audio_is_heard_nothing(client, ids, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("polish_email_body must not run for too-short audio")

    monkeypatch.setattr(main, "polish_email_body", boom)
    resp = client.post(
        "/action/draft_body",
        data={
            "recipient": "alex@example.com",
            "subject": "hi",
            "body": "typed already",
            "body_from_user": "true",
            **ids,
        },
        files={"audio": ("clip.wav", _silence_wav(0.2), "audio/wav")},
    )
    assert resp.status_code == 200
    out = resp.json()
    assert out["error"] == "heard_nothing"
    assert out["body"] is None


def test_draft_body_llm_fail_is_draft_failed_not_empty_string(client, ids, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("ollama down")

    monkeypatch.setattr(main, "polish_email_body", boom)
    resp = client.post(
        "/action/draft_body",
        json={"recipient": "alex@example.com", "subject": "hi", **ids},
    )
    assert resp.status_code == 200
    out = resp.json()
    assert out["error"] == "draft_failed"
    assert out["body"] is None
    assert out["body"] != ""


def test_is_junk_transcript_gates_silence_and_thanks():
    assert whisper.is_junk_transcript("")
    assert whisper.is_junk_transcript(".")
    assert whisper.is_junk_transcript("thank you")
    assert whisper.is_junk_transcript("hi there")  # < 4 words
    assert not whisper.is_junk_transcript(
        "Please let Alex know I will be late to dinner."
    )


def test_transcribe_wav_missing_never_invents_text(monkeypatch):
    monkeypatch.setattr(whisper, "_WhisperModel", None)
    with pytest.raises(whisper.WhisperMissing):
        _REAL_TRANSCRIBE(b"not-a-wav")


def test_transcribe_wav_busy_and_downloading_do_not_block(monkeypatch):
    monkeypatch.setattr(whisper, "_WhisperModel", object)
    assert whisper._lock.acquire(blocking=False)
    try:
        whisper._loading = True
        with pytest.raises(whisper.WhisperDownloading):
            _REAL_TRANSCRIBE(b"xx")
        whisper._loading = False
        with pytest.raises(whisper.WhisperBusy):
            _REAL_TRANSCRIBE(b"xx")
    finally:
        whisper._loading = False
        whisper._lock.release()


def test_attachments_ride_along_on_email_confirm(client, ids, fake_composio, tmp_path):
    attached = tmp_path / "note.txt"
    attached.write_text("hi", encoding="utf-8")
    body = chat(client, "send an email", ids)
    assert body["task_status"] == "AWAITING_INPUT"
    follow = client.post(
        "/chat",
        json={
            "text": "to alex@example.com subject hi body hello",
            "conversation_id": ids["conversation_id"],
            "user_id": ids["user_id"],
            "attachments": [str(attached)],
        },
    ).json()
    assert follow["task_status"] == "PENDING"
    assert follow["resolved_params"]["attachments"] == [str(attached)]
    out = confirm(client, ids, follow["task_id"], "confirm")
    assert out["task_status"] == "SUCCESS"
    assert fake_composio.calls == [
        ("send_email", {**EMAIL_PARAMS, "attachments": [str(attached)]})
    ]


# --- Criterion 7: /action/confirm confirm executes, cancel never does --------


def test_confirm_executes_pending_email(client, ids, fake_composio):
    body = make_pending_email(client, ids)
    out = confirm(client, ids, body["task_id"], "confirm")
    assert out["engine"] == "action"
    assert out["task_status"] == "SUCCESS"
    assert out["confirm_required"] is False
    assert "Sent the email to alex@example.com" in out["answer"]
    assert fake_composio.calls == [("send_email", EMAIL_PARAMS)]
    assert fetch_row(body["task_id"])["status"] == "SUCCESS"


def test_cancel_never_executes(client, ids, fake_composio):
    body = make_pending_email(client, ids)
    out = confirm(client, ids, body["task_id"], "cancel")
    assert out["task_status"] == "CANCELLED"
    assert "Cancelled" in out["answer"]
    assert fake_composio.calls == []  # executor never touched
    assert fetch_row(body["task_id"])["status"] == "CANCELLED"


def test_send_another_after_cancel_starts_blank(client, ids, fake_composio):
    """After Cancel, 'send another email' is a new draft — not the cancelled one."""
    body = make_pending_email(client, ids)
    confirm(client, ids, body["task_id"], "cancel")
    nxt = chat(client, "send another email for me", ids)
    assert nxt["engine"] == "action"
    assert nxt["action_type"] == "send_email"
    assert nxt["task_id"] != body["task_id"]
    assert nxt["task_status"] == "AWAITING_INPUT"
    assert nxt["resolved_params"] == {}
    assert set(nxt["missing_params"]) == {"recipient", "subject", "body"}
    assert fake_composio.calls == []


def test_send_another_after_success_starts_blank(client, ids, fake_composio):
    """Same conversation after a real send (reload case): don't re-offer it."""
    body = make_pending_email(client, ids)
    confirm(client, ids, body["task_id"], "confirm")
    nxt = chat(client, "send an email for me", ids)
    assert nxt["engine"] == "action"
    assert nxt["task_status"] == "AWAITING_INPUT"
    assert nxt["resolved_params"] == {}
    assert nxt["task_id"] != body["task_id"]


# --- Criterion 8: stale/invalid confirms are graceful, no state change -------


def test_confirm_unknown_task_graceful(client, ids):
    out = confirm(client, ids, str(uuid4()), "confirm")
    assert out["task_status"] == "UNKNOWN"
    assert "already done or cancelled" in out["answer"]


def test_confirm_terminal_task_graceful(client, ids, fake_composio):
    body = make_pending_email(client, ids)
    confirm(client, ids, body["task_id"], "cancel")
    fake_composio.calls.clear()
    out = confirm(client, ids, body["task_id"], "confirm")
    assert "already done or cancelled" in out["answer"]
    assert fake_composio.calls == []
    assert fetch_row(body["task_id"])["status"] == "CANCELLED"  # unchanged


def test_confirm_mismatched_user_graceful(client, ids, fake_composio):
    body = make_pending_email(client, ids)
    stranger = {
        "conversation_id": ids["conversation_id"],
        "user_id": str(uuid4()),
    }
    out = confirm(client, stranger, body["task_id"], "confirm")
    assert "already done or cancelled" in out["answer"]
    assert fake_composio.calls == []
    assert fetch_row(body["task_id"])["status"] == "PENDING"  # unchanged


# --- Criterion 9: terminal rows are immutable --------------------------------


def test_terminal_rows_never_mutated(client, ids, fake_open):
    seeded = {
        status: seed_action_with_status(
            ids["user_id"], ids["conversation_id"], status
        )
        for status in actions.TERMINAL_STATUSES
    }
    before = {aid: fetch_row(aid) for aid in seeded.values()}

    for aid in seeded.values():
        # Direct API attempts bounce off terminal rows.
        bounced = actions.update_action(aid, status="PENDING", error="tamper")
        assert bounced["status"] == before[aid]["status"]
        actions.cancel_action(aid)

    # A later /chat action turn in the same conversation leaves them untouched.
    body = chat(client, "open Notes", ids)
    assert body["task_status"] == "SUCCESS"

    for status, aid in seeded.items():
        after = fetch_row(aid)
        assert after == before[aid], f"terminal {status} row was mutated"


# --- Criterion 10: typed yes never confirms; buttons vs memory split ---------


def test_typed_yes_never_confirms_action(client, ids, fake_composio):
    body = make_pending_email(client, ids)
    out = chat(client, "yes", ids)
    assert out["answer"] == main.USE_BUTTONS_REPLY
    assert out["engine"] == "action"
    assert out["task_id"] == body["task_id"]
    assert out["task_status"] == "PENDING"  # no status change
    assert out["confirm_required"] is True
    assert fake_composio.calls == []  # nothing executed
    assert fetch_row(body["task_id"])["status"] == "PENDING"


def test_yes_with_action_and_memory_both_pending(client, ids, fake_composio):
    # The both-pending state is UNREACHABLE via /chat alone in rules mode: a
    # message that triggers the memory ask is an unrelated turn for the open
    # action (cancels it, §10-B), and an action message clears the pending ask.
    # So the action goes in via /chat and the memory ask is seeded directly.
    body = make_pending_email(client, ids)
    long_term_memory.set_pending_memory(
        ids["user_id"],
        ids["conversation_id"],
        [{"key": "reply_style", "value": "shorter replies", "priority": "medium"}],
    )

    out = chat(client, "yes", ids)
    # Typed "yes" answers ONLY the memory ask (§10-A).
    assert out["engine"] == "remember"
    assert out["remembered"] == [
        {"key": "reply_style", "value": "shorter replies", "priority": "medium"}
    ]
    assert_no_task_fields(out)
    facts = long_term_memory.get_facts(ids["user_id"])
    assert any(f["key"] == "reply_style" for f in facts)
    # The action is untouched: still PENDING, never executed.
    assert fetch_row(body["task_id"])["status"] == "PENDING"
    assert fake_composio.calls == []


# --- Criterion 11: unrelated message cancels the open action quietly ---------


def test_nevermind_dismisses_composer_and_asks_to_save_draft(client, ids):
    body = chat(client, "send an email", ids)
    assert body["task_status"] == "AWAITING_INPUT"

    out = chat(client, "nevermind", ids)
    assert out["asked_to_save_draft"] is True
    assert out["answer"] == main.SAVE_DRAFT_ASK
    assert_no_task_fields(out)
    assert fetch_row(body["task_id"])["status"] == "CANCELLED"

    yes = chat(client, "yes", ids)
    assert yes["answer"] == main.SAVE_DRAFT_YES_NOT_READY
    assert yes.get("asked_to_save_draft") is False
    assert_no_task_fields(yes)


def test_nevermind_save_draft_no(client, ids):
    chat(client, "send an email", ids)
    chat(client, "nvm", ids)
    out = chat(client, "no", ids)
    assert out["answer"] == main.SAVE_DRAFT_NO_REPLY
    assert_no_task_fields(out)


def test_awaiting_input_unrelated_message_drops(client, ids):
    body = chat(client, EMAIL_PARTIAL, ids)
    assert body["task_status"] == "AWAITING_INPUT"

    out = chat(client, "what's our refund policy?", ids)
    assert out["engine"] == "retrieve"  # new message handled normally
    assert "(dropped the email draft)" in out["answer"]
    assert_no_task_fields(out)
    assert fetch_row(body["task_id"])["status"] == "CANCELLED"

    nxt = chat(client, "hey", ids)  # never mentioned again
    assert "dropped" not in nxt["answer"].lower()
    assert "email" not in nxt["answer"].lower()


def test_pending_unrelated_message_drops(client, ids, fake_composio):
    body = make_pending_email(client, ids)

    out = chat(client, "tell me a joke", ids)
    assert out["engine"] == "chat"
    assert "(dropped the email draft)" in out["answer"]
    assert_no_task_fields(out)
    assert fetch_row(body["task_id"])["status"] == "CANCELLED"
    assert fake_composio.calls == []

    nxt = chat(client, "hey", ids)
    assert "dropped" not in nxt["answer"].lower()


# --- Criterion 12: new action replaces old; one open row max -----------------


def test_new_action_replaces_pending_action(client, ids, fake_open):
    first = chat(client, EMAIL_FULL, ids)
    assert len(open_rows(ids)) == 1  # invariant after step 1

    second = chat(client, "open Notes", ids)
    assert second["engine"] == "action"
    assert second["task_id"] != first["task_id"]
    assert second["task_status"] == "SUCCESS"
    assert "Dropped the email draft" in second["answer"]
    assert "Opened Notes" in second["answer"]

    rows = {r["action_id"]: r for r in action_rows(ids)}
    assert len(rows) == 2
    assert rows[first["task_id"]]["status"] == "CANCELLED"
    assert rows[second["task_id"]]["status"] == "SUCCESS"
    assert open_rows(ids) == []  # invariant after step 2


def test_new_action_replaces_awaiting_action(client, ids, fake_open):
    first = chat(client, EMAIL_PARTIAL, ids)
    assert first["task_status"] == "AWAITING_INPUT"
    assert len(open_rows(ids)) == 1

    second = chat(client, "open Notes", ids)
    assert second["task_status"] == "SUCCESS"
    assert "Dropped the email draft" in second["answer"]
    assert fetch_row(first["task_id"])["status"] == "CANCELLED"
    assert open_rows(ids) == []


# --- Criterion 13: no COMPOSIO_API_KEY → honest FAILED, never fake success ---


def test_no_composio_key_confirm_fails_gracefully(client, ids, monkeypatch):
    monkeypatch.delenv("COMPOSIO_API_KEY", raising=False)  # belt and braces
    body = make_pending_email(client, ids)  # classify+extract+buttons still work

    out = confirm(client, ids, body["task_id"], "confirm")
    assert out["task_status"] == "FAILED"
    assert out["answer"] == main.COMPOSIO_NOT_CONNECTED_REPLY
    assert out["answer"] != GENERIC_ERROR  # never the generic error string
    assert "Sent the email" not in out["answer"]  # never a claimed success

    row = fetch_row(body["task_id"])
    assert row["status"] == "FAILED"
    assert payload(row)["error"] == "composio_not_connected"


def test_confirm_includes_gmail_connect_link(client, ids, monkeypatch):
    fake = FakeComposio(
        result={
            "ok": False,
            "detail": "no connected account",
            "error": "composio_not_connected",
            "connect_link": "https://connect.composio.dev/link/test",
        }
    )
    monkeypatch.setitem(actions._EXECUTORS, "composio", fake)
    body = make_pending_email(client, ids)
    out = confirm(client, ids, body["task_id"], "confirm")
    assert out["task_status"] == "FAILED"
    assert "https://connect.composio.dev/link/test" in out["answer"]
    assert "Sent the email" not in out["answer"]


def test_for_you_consumer_key_is_not_a_platform_key(monkeypatch):
    monkeypatch.setenv("COMPOSIO_API_KEY", "ck_not_a_platform_project_key")
    assert actions.platform_api_key() is None
    result = actions.ComposioExecutor().execute("send_email", EMAIL_PARAMS)
    assert result["ok"] is False
    assert result["error"] == "composio_not_connected"
    assert "For You" in result["detail"]


def test_for_you_consumer_key_confirm_fails_gracefully(client, ids, monkeypatch):
    monkeypatch.setenv("COMPOSIO_API_KEY", "ck_not_a_platform_project_key")
    body = make_pending_email(client, ids)

    out = confirm(client, ids, body["task_id"], "confirm")
    assert out["task_status"] == "FAILED"
    assert out["answer"] == main.COMPOSIO_NOT_CONNECTED_REPLY
    assert "Sent the email" not in out["answer"]
    assert payload(fetch_row(body["task_id"]))["error"] == "composio_not_connected"


# --- Criterion 14: startup sweep — no replay, terminal rows byte-identical ---


def test_startup_sweep_closes_open_rows_and_executes_nothing(monkeypatch):
    all_statuses = actions.NON_TERMINAL_STATUSES + actions.TERMINAL_STATUSES
    # Separate (user, conversation) per row so create_action's own leftover
    # cancellation can't touch the siblings being seeded.
    seeded = {
        status: seed_action_with_status(str(uuid4()), str(uuid4()), status)
        for status in all_statuses
    }
    before = {aid: fetch_row(aid) for aid in seeded.values()}

    # Prove nothing executes during the sweep.
    monkeypatch.setitem(actions._EXECUTORS, "native", BoomExecutor("native"))
    monkeypatch.setitem(actions._EXECUTORS, "composio", BoomExecutor("composio"))

    # startup_sweep() is exactly what main.py runs at import time, so calling
    # it directly is equivalent to a backend restart against this DB.
    actions.startup_sweep()

    assert fetch_row(seeded["PENDING"])["status"] == "CANCELLED"
    assert fetch_row(seeded["AWAITING_INPUT"])["status"] == "CANCELLED"

    confirmed = fetch_row(seeded["CONFIRMED"])
    assert confirmed["status"] == "FAILED"
    assert payload(confirmed)["error"] == "interrupted_by_restart"

    for status in actions.TERMINAL_STATUSES:
        assert fetch_row(seeded[status]) == before[seeded[status]], (
            f"terminal {status} row changed during startup sweep"
        )


# --- Gmail drafts: autosave, nevermind yes/no, confirm via send_draft --------


def sync_draft(client, ids, recipient, subject, body):
    resp = client.post(
        "/action/sync_draft",
        json={
            "recipient": recipient,
            "subject": subject,
            "body": body,
            **ids,
        },
    )
    assert resp.status_code == 200
    return resp.json()


def test_sync_draft_creates_then_updates_gmail_draft(client, ids, fake_composio):
    first = chat(client, "send an email", ids)
    assert first["task_status"] == "AWAITING_INPUT"

    out = sync_draft(client, ids, "alex@example.com", "hi", "hello")
    assert out["task_status"] == "AWAITING_INPUT"
    assert out["task_id"] == first["task_id"]
    assert fake_composio.draft_calls[0][0] == "create"
    draft_id = payload(fetch_row(first["task_id"]))["gmail_draft_id"]
    assert draft_id in fake_composio.drafts

    again = sync_draft(client, ids, "alex@example.com", "hi", "hello there")
    assert again["task_status"] == "AWAITING_INPUT"
    assert fake_composio.draft_calls[-1][0] == "update"
    assert fake_composio.drafts[draft_id]["body"] == "hello there"
    assert fake_composio.calls == []


def test_sync_draft_skips_until_real_to_address(client, ids, fake_composio):
    chat(client, "send an email", ids)
    out = sync_draft(client, ids, "alex", "hi", "hello")
    assert out["task_status"] == "AWAITING_INPUT"
    assert fake_composio.draft_calls == []
    assert "gmail_draft_id" not in payload(fetch_row(out["task_id"]))


def test_sync_draft_without_open_composer_does_not_create(client, ids, fake_composio):
    out = sync_draft(client, ids, "alex@example.com", "hi", "hello")
    assert out["task_id"] is None
    assert fake_composio.draft_calls == []


def test_nevermind_yes_keeps_gmail_draft(client, ids, fake_composio):
    first = chat(client, "send an email", ids)
    sync_draft(client, ids, "alex@example.com", "hi", "hello")
    draft_id = payload(fetch_row(first["task_id"]))["gmail_draft_id"]

    ask = chat(client, "nevermind", ids)
    assert ask["asked_to_save_draft"] is True
    assert fetch_row(first["task_id"])["status"] == "CANCELLED"

    yes = chat(client, "yes", ids)
    assert yes["answer"] == main.SAVE_DRAFT_YES_REPLY
    assert draft_id in fake_composio.drafts
    assert payload(fetch_row(first["task_id"]))["gmail_draft_kept"] is True
    assert ("delete", draft_id) not in fake_composio.draft_calls
    assert fake_composio.calls == []


def test_nevermind_no_deletes_gmail_draft(client, ids, fake_composio):
    first = chat(client, "send an email", ids)
    sync_draft(client, ids, "alex@example.com", "hi", "hello")
    draft_id = payload(fetch_row(first["task_id"]))["gmail_draft_id"]
    chat(client, "nvm", ids)
    out = chat(client, "no", ids)
    assert out["answer"] == main.SAVE_DRAFT_NO_REPLY
    assert draft_id not in fake_composio.drafts
    assert ("delete", draft_id) in fake_composio.draft_calls


def test_confirm_sends_gmail_draft_not_a_second_email(client, ids, fake_composio):
    first = chat(client, "send an email", ids)
    sync_draft(client, ids, **EMAIL_PARAMS)
    draft_id = payload(fetch_row(first["task_id"]))["gmail_draft_id"]
    pending = client.post(
        "/action/submit_draft",
        json={**EMAIL_PARAMS, **ids},
    ).json()
    assert pending["task_status"] == "PENDING"
    out = confirm(client, ids, pending["task_id"], "confirm")
    assert out["task_status"] == "SUCCESS"
    assert fake_composio.calls == []
    assert ("send", draft_id) in fake_composio.draft_calls
    assert draft_id not in fake_composio.drafts


def test_confirm_cancel_deletes_gmail_draft(client, ids, fake_composio):
    chat(client, "send an email", ids)
    sync_draft(client, ids, **EMAIL_PARAMS)
    pending = client.post(
        "/action/submit_draft",
        json={**EMAIL_PARAMS, **ids},
    ).json()
    draft_id = payload(fetch_row(pending["task_id"]))["gmail_draft_id"]
    out = confirm(client, ids, pending["task_id"], "cancel")
    assert out["task_status"] == "CANCELLED"
    assert draft_id not in fake_composio.drafts
    assert ("delete", draft_id) in fake_composio.draft_calls
    assert fake_composio.calls == []


def test_sync_draft_noconnect_does_not_pretend_success(client, ids, fake_composio):
    fake_composio.connected = False
    first = chat(client, "send an email", ids)
    out = sync_draft(client, ids, **EMAIL_PARAMS)
    assert out["task_status"] == "AWAITING_INPUT"
    assert "gmail_draft_id" not in payload(fetch_row(first["task_id"]))
    assert fake_composio.drafts == {}


def test_session_start_restores_gmail_draft_after_sweep(client, ids, fake_composio):
    first = chat(client, "send an email", ids)
    sync_draft(client, ids, **EMAIL_PARAMS)
    draft_id = payload(fetch_row(first["task_id"]))["gmail_draft_id"]
    actions.startup_sweep()
    assert fetch_row(first["task_id"])["status"] == "CANCELLED"
    assert payload(fetch_row(first["task_id"]))["gmail_draft_id"] == draft_id

    body = client.post("/session/start", json=ids).json()
    assert body["task_status"] == "AWAITING_INPUT"
    assert body["action_type"] == "send_email"
    assert body["task_id"] != first["task_id"]
    assert body["resolved_params"]["recipient"] == "alex@example.com"
    assert payload(fetch_row(body["task_id"]))["gmail_draft_id"] == draft_id
    assert payload(fetch_row(first["task_id"]))["gmail_draft_kept"] is True
    assert ("get", draft_id) in fake_composio.draft_calls


def test_session_start_skips_restore_if_gmail_draft_gone(client, ids, fake_composio):
    first = chat(client, "send an email", ids)
    sync_draft(client, ids, **EMAIL_PARAMS)
    draft_id = payload(fetch_row(first["task_id"]))["gmail_draft_id"]
    fake_composio.drafts.pop(draft_id)
    actions.startup_sweep()
    body = client.post("/session/start", json=ids).json()
    assert "task_id" not in body
    assert payload(fetch_row(first["task_id"])).get("gmail_draft_id") in (None, "")


def test_nevermind_yes_without_composio_is_honest(client, ids):
    chat(client, "send an email", ids)
    sync_draft(client, ids, **EMAIL_PARAMS)
    chat(client, "nevermind", ids)
    yes = chat(client, "yes", ids)
    assert yes["answer"] == main.SAVE_DRAFT_YES_NOT_CONNECTED
    assert "Saved to Gmail" not in yes["answer"]


def keep_draft(client, ids, fake_composio, recipient, subject, body):
    first = chat(client, "send an email", ids)
    sync_draft(client, ids, recipient=recipient, subject=subject, body=body)
    chat(client, "nevermind", ids)
    yes = chat(client, "yes", ids)
    assert yes["answer"] == main.SAVE_DRAFT_YES_REPLY
    draft_id = payload(fetch_row(first["task_id"]))["gmail_draft_id"]
    assert draft_id in fake_composio.drafts
    return first["task_id"], draft_id


def test_send_that_draft_reopens_the_kept_gmail_draft(client, ids, fake_composio):
    _, draft_id = keep_draft(client, ids, fake_composio, **EMAIL_PARAMS)
    out = chat(client, "send that draft", ids)
    assert out["engine"] == "action"
    assert out["task_status"] == "AWAITING_INPUT"
    assert out["action_type"] == "send_email"
    assert payload(fetch_row(out["task_id"]))["gmail_draft_id"] == draft_id
    assert out["resolved_params"]["recipient"] == "alex@example.com"
    assert "Here's that draft" in out["answer"]
    pending = client.post(
        "/action/submit_draft",
        json={**EMAIL_PARAMS, **ids},
    ).json()
    assert pending["task_status"] == "PENDING"
    sent = confirm(client, ids, pending["task_id"], "confirm")
    assert sent["task_status"] == "SUCCESS"
    assert fake_composio.calls == []
    assert ("send", draft_id) in fake_composio.draft_calls
    assert draft_id not in fake_composio.drafts


def test_describe_draft_matches_subject(client, ids, fake_composio):
    keep_draft(
        client, ids, fake_composio,
        recipient="alex@example.com", subject="lunch", body="see you at 1",
    )
    other_ids = {
        "conversation_id": ids["conversation_id"],
        "user_id": ids["user_id"],
    }
    keep_draft(
        client, other_ids, fake_composio,
        recipient="sam@example.com", subject="project", body="status update",
    )
    out = chat(client, "the one about lunch", ids)
    assert out["task_status"] == "AWAITING_INPUT"
    assert out["resolved_params"]["subject"] == "lunch"
    assert out["resolved_params"]["recipient"] == "alex@example.com"


def test_send_that_draft_with_two_asks_which(client, ids, fake_composio):
    keep_draft(
        client, ids, fake_composio,
        recipient="alex@example.com", subject="lunch", body="see you",
    )
    keep_draft(
        client, ids, fake_composio,
        recipient="sam@example.com", subject="project", body="status",
    )
    out = chat(client, "send that draft", ids)
    assert out["task_status"] == "AWAITING_INPUT"
    assert out["resolved_params"]["subject"] == "project"
    assert out["resolved_params"]["recipient"] == "sam@example.com"
    assert "Which one" not in out["answer"]
    assert payload(fetch_row(out["task_id"]))["gmail_draft_id"] in fake_composio.drafts


def test_two_lunch_drafts_asks_which(client, ids, fake_composio):
    keep_draft(
        client, ids, fake_composio,
        recipient="alex@example.com", subject="lunch", body="see you at 1",
    )
    keep_draft(
        client, ids, fake_composio,
        recipient="sam@example.com", subject="lunch plans", body="see you at 2",
    )
    out = chat(client, "the one about lunch", ids)
    assert out.get("task_id") is None
    assert "Which one" in out["answer"]
    assert "alex@example.com" in out["answer"]
    assert "sam@example.com" in out["answer"]
    assert len(open_rows(ids)) == 0


def test_resume_miss_stays_honest(client, ids, fake_composio):
    keep_draft(client, ids, fake_composio, **EMAIL_PARAMS)
    out = chat(client, "the one about zebras", ids)
    assert out["answer"] == main.RESUME_MISS_REPLY
    assert out.get("task_id") is None
    assert "I can't do that yet" not in out["answer"]
    assert len(open_rows(ids)) == 0


def test_send_an_email_still_starts_blank_when_a_draft_is_kept(client, ids, fake_composio):
    keep_draft(client, ids, fake_composio, **EMAIL_PARAMS)
    out = chat(client, "send an email", ids)
    assert out["task_status"] == "AWAITING_INPUT"
    assert out["resolved_params"] == {}
    assert payload(fetch_row(out["task_id"])).get("gmail_draft_id") in (None, "")


def test_gmail_native_draft_resume_without_actions_log(client, ids, fake_composio):
    fake_composio.drafts["r-native-lunch"] = {
        "recipient": "pat@example.com",
        "subject": "lunch",
        "body": "see you at 1",
        "updated_at": "2026-08-26T12:00:00Z",
    }
    assert action_rows(ids) == []
    out = chat(client, "finish the lunch email", ids)
    assert out["engine"] == "action"
    assert out["task_status"] == "AWAITING_INPUT"
    assert out["action_type"] == "send_email"
    assert out["resolved_params"]["recipient"] == "pat@example.com"
    assert out["resolved_params"]["subject"] == "lunch"
    row_payload = payload(fetch_row(out["task_id"]))
    assert row_payload["gmail_draft_id"] == "r-native-lunch"
    assert row_payload["mail_provider"] == "gmail"
    assert row_payload["mail_draft_id"] == "r-native-lunch"
    assert ("list", 50) in fake_composio.draft_calls
    assert ("get", "r-native-lunch") in fake_composio.draft_calls
    pending = client.post(
        "/action/submit_draft",
        json={
            "recipient": "pat@example.com",
            "subject": "lunch",
            "body": "see you at 1",
            **ids,
        },
    ).json()
    assert pending["task_status"] == "PENDING"
    sent = confirm(client, ids, pending["task_id"], "confirm")
    assert sent["task_status"] == "SUCCESS"
    assert fake_composio.calls == []
    assert ("send", "r-native-lunch") in fake_composio.draft_calls
    assert "r-native-lunch" not in fake_composio.drafts


def test_resume_empty_mailbox_is_miss(client, ids, fake_composio):
    assert fake_composio.drafts == {}
    out = chat(client, "send that draft", ids)
    assert out["answer"] == main.RESUME_MISS_REPLY
    assert out.get("task_id") is None
    assert len(open_rows(ids)) == 0
    assert ("list", 50) in fake_composio.draft_calls


def test_resume_without_composio_does_not_invent_mailbox(client, ids):
    out = chat(client, "send that draft", ids)
    assert out["answer"] == main.RESUME_MISS_REPLY
    assert out.get("task_id") is None
    assert len(open_rows(ids)) == 0


def test_unsynced_local_draft_still_resumes(client, ids, fake_composio):
    first = chat(client, "send an email", ids)
    sync_draft(client, ids, "alex", "lunch", "see you at 1")
    assert "gmail_draft_id" not in payload(fetch_row(first["task_id"]))
    actions.startup_sweep()
    assert fetch_row(first["task_id"])["status"] == "CANCELLED"
    out = chat(client, "finish the lunch email", ids)
    assert out["task_status"] == "AWAITING_INPUT"
    assert out["resolved_params"]["subject"] == "lunch"
    assert payload(fetch_row(out["task_id"])).get("gmail_draft_id") in (None, "")
    assert len(open_rows(ids)) == 1


def _gmail_b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def _native_gmail_draft(to, subject, body, *, snippet=""):
    payload = {
        "headers": [
            {"name": "To", "value": to},
            {"name": "Subject", "value": subject},
        ],
        "mimeType": "multipart/alternative",
        "body": {"size": 0},
        "parts": [
            {"mimeType": "text/plain", "body": {"data": _gmail_b64(body)}},
        ],
    }
    message = {"payload": payload}
    if snippet:
        message["snippet"] = snippet
    return {
        "message": message,
        "updated_at": "2026-08-26T12:00:00Z",
    }


def test_resume_get_reads_gmail_mime_parts(client, ids, fake_composio):
    body_text = "please find my statement of purpose attached"
    fake_composio.drafts["r-mime"] = _native_gmail_draft(
        "admissions@example.com", "application", body_text
    )
    out = chat(client, "send that draft", ids)
    assert out["engine"] == "action"
    assert out["task_status"] == "AWAITING_INPUT"
    assert out["resolved_params"]["recipient"] == "admissions@example.com"
    assert out["resolved_params"]["subject"] == "application"
    assert out["resolved_params"]["body"] == body_text
    assert ("get", "r-mime") in fake_composio.draft_calls


def test_show_me_the_draft_matches_body_not_subject(client, ids, fake_composio):
    body_text = "this is my math tutor application for the fall cohort"
    fake_composio.drafts["r-tutor"] = _native_gmail_draft(
        "hire@example.com",
        "FYI",
        body_text,
        snippet=body_text,
    )
    text = "can you show me the draft about math tutor application"
    assert intent.classify_intent(text)["intent"] == "ACTION"
    analysis = intent.analyze_action_request(text)
    assert analysis["resume"] is True
    assert analysis["action_type"] == "send_email"
    out = chat(client, text, ids)
    assert out["engine"] == "action"
    assert out["task_status"] == "AWAITING_INPUT"
    assert out["resolved_params"]["subject"] == "FYI"
    assert out["resolved_params"]["recipient"] == "hire@example.com"
    assert "math tutor application" in out["resolved_params"]["body"]
    assert payload(fetch_row(out["task_id"]))["gmail_draft_id"] == "r-tutor"


def test_resume_get_raw_rfc2822_fills_body(client, ids, monkeypatch):
    body_text = "I am applying to be a math tutor"
    rfc = (
        "To: hire@example.com\r\n"
        "Subject: FYI\r\n"
        "Content-Type: text/plain; charset=UTF-8\r\n"
        "\r\n"
        f"{body_text}\r\n"
    )
    full_raw = {
        "data": {
            "id": "r-raw",
            "message": {
                "payload": {
                    "headers": [
                        {"name": "To", "value": "hire@example.com"},
                        {"name": "Subject", "value": "FYI"},
                    ],
                    "mimeType": "multipart/alternative",
                    "body": {"size": 0},
                    "parts": [
                        {
                            "mimeType": "text/plain",
                            "body": {"attachmentId": "att-1", "size": 200},
                        }
                    ],
                }
            },
        }
    }
    calls = []
    executor = actions.ComposioExecutor()

    def _run_tool(slug, arguments, **kwargs):
        calls.append((slug, dict(arguments)))
        if slug == "GMAIL_LIST_DRAFTS":
            return {
                "ok": True,
                "raw": {
                    "drafts": [
                        {
                            "id": "r-raw",
                            "message": {
                                "payload": {
                                    "headers": [
                                        {"name": "To", "value": "hire@example.com"},
                                        {"name": "Subject", "value": "FYI"},
                                    ],
                                    "parts": [
                                        {
                                            "mimeType": "text/plain",
                                            "body": {"attachmentId": "att-1"},
                                        }
                                    ],
                                }
                            },
                        }
                    ]
                },
            }
        assert slug == "GMAIL_GET_DRAFT"
        fmt = arguments.get("format")
        if fmt == "full":
            return {"ok": True, "raw": full_raw}
        assert fmt == "raw"
        return {
            "ok": True,
            "raw": {"data": {"id": "r-raw", "message": {"raw": _gmail_b64(rfc)}}},
        }

    monkeypatch.setattr(executor, "_run_tool", _run_tool)
    monkeypatch.setitem(actions._EXECUTORS, "composio", executor)
    monkeypatch.delenv("COMPOSIO_API_KEY", raising=False)
    out = chat(client, "send that draft", ids)
    assert out["engine"] == "action"
    assert out["task_status"] == "AWAITING_INPUT"
    assert out["resolved_params"]["recipient"] == "hire@example.com"
    assert out["resolved_params"]["subject"] == "FYI"
    assert out["resolved_params"]["body"].strip() == body_text
    formats = [args.get("format") for slug, args in calls if slug == "GMAIL_GET_DRAFT"]
    assert formats == ["full", "raw"]


def test_resume_body_from_list_snippet_when_get_empty(client, ids, fake_composio):
    snippet = "math tutor application for the fall"
    fake_composio.drafts["r-snip"] = {
        "snippet": snippet,
        "updated_at": "1",
        "get": {
            "payload": {
                "headers": [
                    {"name": "To", "value": "hire@example.com"},
                    {"name": "Subject", "value": "FYI"},
                ],
                "body": {"size": 0},
                "parts": [
                    {
                        "mimeType": "text/plain",
                        "body": {"attachmentId": "att-1"},
                    }
                ],
            }
        },
    }
    out = chat(client, "send that draft", ids)
    assert out["engine"] == "action"
    assert out["resolved_params"]["recipient"] == "hire@example.com"
    assert out["resolved_params"]["subject"] == "FYI"
    assert out["resolved_params"]["body"] == snippet
    assert ("get", "r-snip") in fake_composio.draft_calls


def test_moon_draft_beats_identical_urgent_which_one(client, ids, fake_composio):
    fake_composio.drafts["r-u1"] = {
        "recipient": "nate@example.com",
        "subject": "[urgent]",
        "body": "",
        "updated_at": "1",
    }
    fake_composio.drafts["r-u2"] = {
        "recipient": "nate@example.com",
        "subject": "[urgent]",
        "body": "",
        "updated_at": "2",
    }
    fake_composio.drafts["r-moon"] = {
        "recipient": "sd@nothing.com",
        "subject": "trip",
        "body": "going to the moon next week",
        "updated_at": "3",
    }
    out = chat(client, "can you pull up the email draft about going to the moon", ids)
    assert out["engine"] == "action"
    assert out["task_status"] == "AWAITING_INPUT"
    assert "Which one" not in out["answer"]
    assert out["resolved_params"]["recipient"] == "sd@nothing.com"
    assert "moon" in out["resolved_params"]["body"]
    assert payload(fetch_row(out["task_id"]))["gmail_draft_id"] == "r-moon"


def test_which_one_followup_address_is_not_open_app(client, ids, fake_composio):
    fake_composio.drafts["r-alex"] = {
        "recipient": "alex@example.com",
        "subject": "lunch",
        "body": "see you at 1",
        "updated_at": "1",
    }
    fake_composio.drafts["r-sd"] = {
        "recipient": "sd@nothing.com",
        "subject": "lunch",
        "body": "see you at 2",
        "updated_at": "2",
    }
    first = chat(client, "the one about lunch", ids)
    assert first.get("task_id") is None
    assert "Which one" in first["answer"]
    assert "sd@nothing.com" in first["answer"]
    out = chat(client, "it was to sd@nothing.com", ids)
    assert out["engine"] == "action"
    assert out["action_type"] == "send_email"
    assert out["task_status"] == "AWAITING_INPUT"
    assert out["resolved_params"]["recipient"] == "sd@nothing.com"
    assert out.get("missing_params") != ["app_name"]
    assert out.get("missing_params") != ["app_names"]


def test_which_one_none_clears_pick(client, ids, fake_composio):
    fake_composio.drafts["r-alex"] = {
        "recipient": "alex@example.com",
        "subject": "lunch",
        "body": "see you at 1",
        "updated_at": "1",
    }
    fake_composio.drafts["r-sam"] = {
        "recipient": "sam@example.com",
        "subject": "lunch",
        "body": "see you at 2",
        "updated_at": "2",
    }
    first = chat(client, "the one about lunch", ids)
    assert "Which one" in first["answer"]
    out = chat(client, "none", ids)
    assert out.get("task_id") is None
    assert out["engine"] == "chat"
    assert "leave those" in out["answer"].lower() or "okay" in out["answer"].lower()
    assert len(open_rows(ids)) == 0


# --- Session working context (per-conversation pad, not long-term facts) -----


def test_quit_those_uses_session_pad(client, ids, fake_open):
    opened = chat(client, "open Notes and Safari", ids)
    assert opened["task_status"] == "SUCCESS"
    assert long_term_memory.get_facts(ids["user_id"]) == []
    body = chat(client, "quit those", ids)
    assert body["engine"] == "action"
    assert body["action_type"] == "quit_app"
    assert body["task_status"] == "PENDING"
    assert body["resolved_params"]["app_names"] == ["Notes", "Safari"]
    assert "Notes" in body["answer"]
    assert "Safari" in body["answer"]
    assert native_action_calls(fake_open) == [
        ["open", "-a", "Notes"],
        ["open", "-a", "Safari"],
    ]
    assert long_term_memory.get_facts(ids["user_id"]) == []


def test_close_it_uses_single_session_app(client, ids, fake_open):
    chat(client, "open Notes", ids)
    body = chat(client, "close it", ids)
    assert body["engine"] == "action"
    assert body["action_type"] == "close_app"
    assert body["task_status"] == "SUCCESS"
    assert body["resolved_params"]["app_names"] == ["Notes"]
    assert native_action_calls(fake_open)[-1][:2] == ["osascript", "-e"]
    assert long_term_memory.get_facts(ids["user_id"]) == []


def test_close_it_does_not_guess_among_two(client, ids, fake_open):
    chat(client, "open Notes and Safari", ids)
    body = chat(client, "close it", ids)
    assert body["engine"] == "action"
    assert body["action_type"] == "close_app"
    assert body["task_status"] == "AWAITING_INPUT"
    assert "app_names" in (body.get("missing_params") or [])
    names = (body.get("resolved_params") or {}).get("app_names") or []
    assert names == []
    assert native_action_calls(fake_open) == [
        ["open", "-a", "Notes"],
        ["open", "-a", "Safari"],
    ]
    assert long_term_memory.get_facts(ids["user_id"]) == []


def test_session_pad_does_not_follow_new_conversation(client, ids, fake_open):
    chat(client, "open Notes and Safari", ids)
    other = {"user_id": ids["user_id"], "conversation_id": str(uuid4())}
    body = chat(client, "quit those", other)
    names = (body.get("resolved_params") or {}).get("app_names") or []
    assert "Notes" not in names
    assert "Safari" not in names
    assert body.get("task_status") != "PENDING"
    assert long_term_memory.get_facts(ids["user_id"]) == []


def test_explicit_quit_name_beats_session_pad(client, ids, fake_open):
    chat(client, "open Notes", ids)
    body = chat(client, "quit Safari", ids)
    assert body["engine"] == "action"
    assert body["action_type"] == "quit_app"
    assert body["task_status"] == "PENDING"
    assert body["resolved_params"]["app_names"] == ["Safari"]
    assert "Notes" not in body["answer"]
    assert long_term_memory.get_facts(ids["user_id"]) == []
