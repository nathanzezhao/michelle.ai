"""User-shaped e2e flows — what a person actually types, including typos.

HTTP /chat only. Temp DB + mock LLM + rules intent (conftest). No live Gmail,
no real `open`/`osascript`, no Playwright.

IDs E1–E20 map to Ray's Ada report.
"""

from uuid import uuid4

import pytest

import actions
import session_context
import whisper
from conftest import chat
from test_r1_actions import (
    FakeComposio,
    FakeSubprocess,
    confirm,
    native_action_calls,
    payload,
    fetch_row,
)

CLOSE_NOTES = ["osascript", "-e", 'tell application "Notes" to close every window']
CLOSE_SAFARI = ["osascript", "-e", 'tell application "Safari" to close every window']
OPEN_NOTES = ["open", "-a", "Notes"]
OPEN_SAFARI = ["open", "-a", "Safari"]


@pytest.fixture(autouse=True)
def _no_real_side_effects(monkeypatch):
    def _blocked(*args, **kwargs):
        raise AssertionError("real subprocess.run reached during e2e user-flow tests")

    monkeypatch.setattr(actions.subprocess, "run", _blocked)
    monkeypatch.delenv("COMPOSIO_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def _no_real_whisper(monkeypatch):
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


def _list_calls(fake):
    return [c for c in fake.draft_calls if c and c[0] == "list"]


def _seed_pad_gmail(ids, fake_composio, extra_drafts=None):
    fake_composio.drafts["r-pad"] = {
        "recipient": "alex@example.com",
        "subject": "hello",
        "body": "from this chat",
        "updated_at": "1",
    }
    if extra_drafts:
        fake_composio.drafts.update(extra_drafts)
    session_context.record_action(
        ids["user_id"],
        ids["conversation_id"],
        "send_email",
        last_draft={
            "provider": "gmail",
            "remote_id": "r-pad",
            "recipient": "alex@example.com",
            "subject": "hello",
            "body": "from this chat",
        },
    )
    fake_composio.draft_calls.clear()


def _open_notes_and_safari(client, ids):
    opened = chat(client, "open Notes and Safari", ids)
    assert opened["engine"] == "action"
    assert opened["action_type"] == "open_app"
    assert opened["task_status"] == "SUCCESS"
    assert opened["resolved_params"]["app_names"] == ["Notes", "Safari"]
    return opened


# --- Apps --------------------------------------------------------------------


def test_e1_now_close_them(client, ids, fake_open):
    _open_notes_and_safari(client, ids)
    body = chat(client, "now close them", ids)
    assert body["engine"] == "action"
    assert body["action_type"] == "close_app"
    assert body["task_status"] == "SUCCESS"
    assert body["resolved_params"]["app_names"] == ["Notes", "Safari"]
    assert native_action_calls(fake_open) == [
        OPEN_NOTES,
        OPEN_SAFARI,
        CLOSE_NOTES,
        CLOSE_SAFARI,
    ]
    assert "[mock mode]" not in body["answer"]
    assert "I heard:" not in body["answer"]


def test_e2_clsoe_it(client, ids, fake_open):
    opened = chat(client, "open Notes", ids)
    assert opened["task_status"] == "SUCCESS"
    body = chat(client, "clsoe it", ids)
    assert body["engine"] == "action"
    assert body["action_type"] == "close_app"
    assert body["task_status"] == "SUCCESS"
    assert body["resolved_params"]["app_names"] == ["Notes"]
    assert native_action_calls(fake_open) == [OPEN_NOTES, CLOSE_NOTES]


def test_e3_quti_those(client, ids, fake_open):
    _open_notes_and_safari(client, ids)
    body = chat(client, "quti those", ids)
    assert body["engine"] == "action"
    assert body["action_type"] == "quit_app"
    assert body["task_status"] == "PENDING"
    assert body["confirm_required"] is True
    assert body["resolved_params"]["app_names"] == ["Notes", "Safari"]
    assert "Notes" in body["answer"]
    assert "Safari" in body["answer"]
    assert "Ready to quit" in body["answer"]
    assert native_action_calls(fake_open) == [OPEN_NOTES, OPEN_SAFARI]


def test_e4_pls_close_them(client, ids, fake_open):
    _open_notes_and_safari(client, ids)
    body = chat(client, "pls close them", ids)
    assert body["engine"] == "action"
    assert body["action_type"] == "close_app"
    assert body["task_status"] == "SUCCESS"
    assert body["resolved_params"]["app_names"] == ["Notes", "Safari"]
    assert native_action_calls(fake_open) == [
        OPEN_NOTES,
        OPEN_SAFARI,
        CLOSE_NOTES,
        CLOSE_SAFARI,
    ]


def test_e5_close_it_two_apps_asks_which(client, ids, fake_open):
    _open_notes_and_safari(client, ids)
    body = chat(client, "close it", ids)
    assert body["engine"] == "action"
    assert body["action_type"] == "close_app"
    assert body["task_status"] == "AWAITING_INPUT"
    assert "app_names" in (body.get("missing_params") or [])
    names = (body.get("resolved_params") or {}).get("app_names") or []
    assert names == []
    assert native_action_calls(fake_open) == [OPEN_NOTES, OPEN_SAFARI]


def test_e6_close_it_one_app(client, ids, fake_open):
    opened = chat(client, "open Notes", ids)
    assert opened["task_status"] == "SUCCESS"
    body = chat(client, "close it", ids)
    assert body["engine"] == "action"
    assert body["action_type"] == "close_app"
    assert body["task_status"] == "SUCCESS"
    assert body["resolved_params"]["app_names"] == ["Notes"]
    assert native_action_calls(fake_open) == [OPEN_NOTES, CLOSE_NOTES]


def test_e7_quit_those_does_not_follow_new_conversation(client, ids, fake_open):
    _open_notes_and_safari(client, ids)
    other = {"user_id": ids["user_id"], "conversation_id": str(uuid4())}
    body = chat(client, "quit those", other)
    names = (body.get("resolved_params") or {}).get("app_names") or []
    assert "Notes" not in names
    assert "Safari" not in names
    assert body.get("task_status") != "PENDING"
    assert "Ready to quit Notes" not in (body.get("answer") or "")
    assert native_action_calls(fake_open) == [OPEN_NOTES, OPEN_SAFARI]


def test_e13_clsoe_those(client, ids, fake_open):
    _open_notes_and_safari(client, ids)
    body = chat(client, "clsoe those", ids)
    assert body["engine"] == "action"
    assert body["action_type"] == "close_app"
    assert body["task_status"] == "SUCCESS"
    assert body["resolved_params"]["app_names"] == ["Notes", "Safari"]
    assert native_action_calls(fake_open) == [
        OPEN_NOTES,
        OPEN_SAFARI,
        CLOSE_NOTES,
        CLOSE_SAFARI,
    ]


# --- Draft -------------------------------------------------------------------


def test_e8_show_me_the_draft_uses_pad_not_list(client, ids, fake_composio):
    _seed_pad_gmail(
        ids,
        fake_composio,
        extra_drafts={
            "r-other": {
                "recipient": "sam@example.com",
                "subject": "newer",
                "body": "mailbox newest",
                "updated_at": "9",
            }
        },
    )
    out = chat(client, "show me the draft", ids)
    assert out["engine"] == "action"
    assert out["action_type"] == "send_email"
    assert out["task_status"] == "AWAITING_INPUT"
    assert out["resolved_params"]["recipient"] == "alex@example.com"
    assert out["resolved_params"]["subject"] == "hello"
    assert payload(fetch_row(out["task_id"]))["gmail_draft_id"] == "r-pad"
    assert _list_calls(fake_composio) == []
    assert ("get", "r-pad") in fake_composio.draft_calls


def test_e9_send_that_draft_uses_pad_not_list(client, ids, fake_composio):
    _seed_pad_gmail(
        ids,
        fake_composio,
        extra_drafts={
            "r-other": {
                "recipient": "sam@example.com",
                "subject": "newer",
                "body": "mailbox newest",
                "updated_at": "9",
            }
        },
    )
    out = chat(client, "send that draft", ids)
    assert out["engine"] == "action"
    assert out["action_type"] == "send_email"
    assert out["task_status"] == "AWAITING_INPUT"
    assert out["resolved_params"]["recipient"] == "alex@example.com"
    assert out["resolved_params"]["subject"] == "hello"
    assert payload(fetch_row(out["task_id"]))["gmail_draft_id"] == "r-pad"
    assert _list_calls(fake_composio) == []
    assert ("get", "r-pad") in fake_composio.draft_calls


def test_e10_the_one_about_lunch_still_lists(client, ids, fake_composio):
    _seed_pad_gmail(
        ids,
        fake_composio,
        extra_drafts={
            "r-lunch": {
                "recipient": "sam@example.com",
                "subject": "lunch",
                "body": "see you at 1",
                "updated_at": "2",
            }
        },
    )
    out = chat(client, "the one about lunch", ids)
    assert out["engine"] == "action"
    assert out["action_type"] == "send_email"
    assert out["task_status"] == "AWAITING_INPUT"
    assert out["resolved_params"]["subject"] == "lunch"
    assert out["resolved_params"]["recipient"] == "sam@example.com"
    assert payload(fetch_row(out["task_id"]))["gmail_draft_id"] == "r-lunch"
    assert _list_calls(fake_composio)


def test_e11_close_that_email_resumes_draft_not_mail(
    client, ids, fake_open, fake_composio
):
    _seed_pad_gmail(ids, fake_composio)
    out = chat(client, "close that email", ids)
    assert out["engine"] == "action"
    assert out.get("action_type") != "close_app"
    mail_closes = [
        argv
        for argv in native_action_calls(fake_open)
        if argv[:2] == ["osascript", "-e"] and "Mail" in argv[2]
    ]
    assert mail_closes == []
    assert "action_type" in out, (
        "spec: close that email resumes this-chat last_draft as send_email; "
        f"got keys={sorted(out)} answer={out.get('answer')!r} "
        f"list_calls={_list_calls(fake_composio)} draft_calls={fake_composio.draft_calls}"
    )
    assert out["action_type"] == "send_email"
    assert out["task_status"] == "AWAITING_INPUT"
    assert out["resolved_params"]["recipient"] == "alex@example.com"
    assert payload(fetch_row(out["task_id"]))["gmail_draft_id"] == "r-pad"
    assert _list_calls(fake_composio) == []


def test_e12_send_an_emauil_complete_goes_pending(client, ids):
    body = chat(
        client,
        "send an emauil to alex@example.com subject hi body hello",
        ids,
    )
    assert body["engine"] == "action"
    assert body["action_type"] == "send_email"
    assert body["task_status"] == "PENDING"
    assert body["confirm_required"] is True
    assert body["resolved_params"]["recipient"] == "alex@example.com"
    assert body["resolved_params"]["subject"] == "hi"
    assert body["resolved_params"]["body"] == "hello"
    assert body["missing_params"] == []


def test_e14_open_it_again_reopens_pad_app(client, ids, fake_open):
    chat(client, "open Notes", ids)
    fake_open.calls.clear()
    body = chat(client, "open it again", ids)
    assert body["engine"] == "action"
    assert body["action_type"] == "open_app"
    assert body["task_status"] == "SUCCESS"
    assert body["resolved_params"]["app_names"] == ["Notes"]
    assert native_action_calls(fake_open) == [OPEN_NOTES]


def test_e15_open_it_agian_typo_still_uses_pad(client, ids, fake_open):
    chat(client, "open Notes", ids)
    fake_open.calls.clear()
    body = chat(client, "open it agian", ids)
    assert body["task_status"] == "SUCCESS"
    assert body["resolved_params"]["app_names"] == ["Notes"]
    assert native_action_calls(fake_open) == [OPEN_NOTES]


def test_e16_open_it_safari_is_explicit(client, ids, fake_open):
    chat(client, "open Notes", ids)
    fake_open.calls.clear()
    body = chat(client, "open it Safari", ids)
    assert body["task_status"] == "SUCCESS"
    assert body["resolved_params"]["app_names"] == ["Safari"]
    assert native_action_calls(fake_open) == [OPEN_SAFARI]


def test_e17_open_it_again_two_apps_does_not_guess(client, ids, fake_open):
    chat(client, "open Notes and Safari", ids)
    fake_open.calls.clear()
    body = chat(client, "open it again", ids)
    assert body["engine"] == "action"
    assert body["action_type"] == "open_app"
    assert body["task_status"] == "AWAITING_INPUT"
    assert "app_names" in (body.get("missing_params") or [])
    assert native_action_calls(fake_open) == []


def test_e18_open_visual_studio_code_then_close_it(client, ids, fake_open, monkeypatch):
    fake_open.running = list(fake_open.running) + ["Code"]
    monkeypatch.setattr(
        actions,
        "_list_installed_app_names",
        lambda: ["Visual Studio Code", "Safari", "Notes"],
    )
    opened = chat(client, "open visual studio code", ids)
    assert opened["engine"] == "action"
    assert opened["action_type"] == "open_app"
    assert opened["task_status"] == "SUCCESS"
    pad = session_context.get(ids["user_id"], ids["conversation_id"])
    assert pad["last_app_names"] == ["Code"]
    fake_open.calls.clear()
    body = chat(client, "close it", ids)
    assert body["engine"] == "action"
    assert body["action_type"] == "close_app"
    assert body["task_status"] == "SUCCESS"
    assert "isn't open" not in body["answer"].lower()
    assert "Closed Code" in body["answer"]
    assert native_action_calls(fake_open) == [
        ["osascript", "-e", 'tell application "Code" to close every window']
    ]


def test_e19_open_outlook_then_close_it(client, ids, fake_open, monkeypatch):
    fake_open.running = list(fake_open.running) + ["Microsoft Outlook"]
    monkeypatch.setattr(
        actions,
        "_list_installed_app_names",
        lambda: ["Microsoft Outlook", "Safari", "Notes"],
    )
    opened = chat(client, "open outlook", ids)
    assert opened["engine"] == "action"
    assert opened["action_type"] == "open_app"
    assert opened["task_status"] == "SUCCESS"
    assert native_action_calls(fake_open) == [["open", "-a", "Microsoft Outlook"]]
    pad = session_context.get(ids["user_id"], ids["conversation_id"])
    assert pad["last_app_names"] == ["Microsoft Outlook"]
    fake_open.calls.clear()
    body = chat(client, "close it", ids)
    assert body["engine"] == "action"
    assert body["action_type"] == "close_app"
    assert body["task_status"] == "SUCCESS"
    assert "isn't open" not in body["answer"].lower()
    assert "outlook" not in (body.get("resolved_params") or {}).get("app_names", [])
    assert native_action_calls(fake_open) == [
        ["osascript", "-e", 'tell application "Microsoft Outlook" to close every window']
    ]


def test_e20_open_outlook_then_quit_it(client, ids, fake_open, monkeypatch):
    fake_open.running = list(fake_open.running) + ["Microsoft Outlook"]
    monkeypatch.setattr(
        actions,
        "_list_installed_app_names",
        lambda: ["Microsoft Outlook", "Safari", "Notes"],
    )
    opened = chat(client, "open outlook", ids)
    assert opened["engine"] == "action"
    assert opened["action_type"] == "open_app"
    assert opened["task_status"] == "SUCCESS"
    pad = session_context.get(ids["user_id"], ids["conversation_id"])
    assert pad["last_app_names"] == ["Microsoft Outlook"]
    fake_open.calls.clear()
    body = chat(client, "quit it", ids)
    assert body["engine"] == "action"
    assert body["action_type"] == "quit_app"
    assert body["task_status"] == "PENDING"
    assert body["confirm_required"] is True
    assert body["resolved_params"]["app_names"] == ["Microsoft Outlook"]
    assert native_action_calls(fake_open) == []
    out = confirm(client, ids, body["task_id"], "confirm")
    assert out["task_status"] == "SUCCESS"
    assert native_action_calls(fake_open) == [
        ["osascript", "-e", 'tell application "Microsoft Outlook" to quit']
    ]
