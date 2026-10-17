"""CHAT, RETRIEVE, and REMEMBER read the working pad. Email polish does not."""

import session_context
from conftest import chat
from llm import EMAIL_BODY_POLISH_SYSTEM, _build_system_prompt, ask_llm


def _notes_pad(ids):
    session_context.record_action(
        ids["user_id"],
        ids["conversation_id"],
        "open_app",
        app_names=["Notes"],
    )
    return session_context.get(ids["user_id"], ids["conversation_id"])


def test_system_prompt_includes_working_pad():
    prompt = _build_system_prompt(
        None,
        {
            "last_opened": ["Notes"],
            "last_closed": [],
            "last_quit": [],
            "last_app_names": ["Notes"],
            "last_action_type": "open_app",
            "last_draft": {"provider": "gmail", "recipient": "sam@example.com"},
            "last_draft_pick": None,
        },
    )
    assert "Notes" in prompt
    assert "sam@example.com" in prompt


def test_empty_pad_does_not_change_system_prompt():
    bare = _build_system_prompt(None)
    empty = _build_system_prompt(None, session_context._empty())
    assert empty == bare


def test_mock_chat_reads_opened_app_from_pad():
    answer = ask_llm(
        "what did you just open",
        [],
        [],
        {"last_opened": ["Notes"], "last_draft": None},
    )
    assert "Notes" in answer


def test_mock_greeting_stays_a_greeting_when_pad_is_set():
    answer = ask_llm(
        "hey",
        [],
        [],
        {"last_opened": ["Notes"], "last_draft": None},
    )
    assert answer == "Hi! I'm Michelle in mock mode. No API credits are being used."


def test_chat_route_reads_session_pad(client, ids):
    _notes_pad(ids)
    resp = client.post(
        "/chat",
        json={
            "text": "what did you just open",
            "force_intent": "CHAT",
            **ids,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["intent"] == "CHAT"
    assert "Notes" in body["answer"]


def test_remember_route_reads_session_pad(client, ids, monkeypatch):
    pad = _notes_pad(ids)
    seen = {}

    def _capture(prompt, history=None, long_term_facts=None, session_context=None):
        seen["pad"] = session_context
        return "Nathan"

    monkeypatch.setattr("main.ask_llm", _capture)
    resp = client.post(
        "/chat",
        json={
            "text": "what's my name",
            "force_intent": "REMEMBER",
            **ids,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["intent"] == "REMEMBER"
    assert seen["pad"]["last_opened"] == pad["last_opened"]


def test_retrieve_route_reads_session_pad(client, ids, monkeypatch):
    pad = _notes_pad(ids)
    seen = {}

    def _capture(prompt, context, history=None, long_term_facts=None, session_context=None):
        seen["pad"] = session_context
        seen["context"] = context
        return "From the docs."

    monkeypatch.setattr("main.ask_llm_with_context", _capture)
    resp = client.post(
        "/chat",
        json={
            "text": "what's the refund policy",
            "force_intent": "RETRIEVE",
            **ids,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["intent"] == "RETRIEVE"
    assert seen["pad"]["last_opened"] == pad["last_opened"]
    assert "QUOKKABERRY-77" in seen["context"]


def test_email_polish_prompt_has_no_session_pad():
    assert "Notes" not in EMAIL_BODY_POLISH_SYSTEM
    assert "working pad" not in EMAIL_BODY_POLISH_SYSTEM.lower()


def test_hey_without_pad_unchanged(client, ids):
    body = chat(client, "hey", ids)
    assert (
        body["answer"]
        == "Hi! I'm Michelle in mock mode. No API credits are being used."
    )
