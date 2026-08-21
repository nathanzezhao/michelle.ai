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

import json
import sqlite3
import types
from uuid import uuid4

import pytest

import actions
import intent
import long_term_memory
import main
from conftest import chat

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


def assert_no_task_fields(body):
    for field in TASK_FIELDS:
        assert field not in body, f"non-action turn leaked task field {field!r}"


# --- Fakes -------------------------------------------------------------------


class FakeSubprocess:
    """Stand-in for subprocess.run: records argv, never touches macOS."""

    def __init__(self):
        self.calls = []
        self.returncode = 0

    def run(self, argv, **kwargs):
        # NativeExecutor must pass args as a list, never a shell string (§5).
        assert isinstance(argv, list)
        assert argv[:2] == ["open", "-a"]
        self.calls.append(argv)
        return types.SimpleNamespace(
            returncode=self.returncode, stdout="", stderr=""
        )


class FakeComposio:
    """Recording composio executor; result configurable per test."""

    def __init__(self, result=None):
        self.calls = []
        self.result = result or {"ok": True, "detail": "email sent", "error": None}

    def execute(self, action_type, params):
        self.calls.append((action_type, dict(params)))
        return self.result


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


@pytest.fixture
def fake_open(monkeypatch):
    fake = FakeSubprocess()
    monkeypatch.setattr(actions.subprocess, "run", fake.run)
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
