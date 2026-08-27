"""Suite 8 — unit tests on the deterministic seams (no HTTP)."""

import base64
from uuid import uuid4

import pytest

import actions
import long_term_memory
import retrieve
from intent import (
    _looks_like_composer_dismiss,
    _looks_like_question,
    _looks_like_resume_draft,
    analyze_action_request,
    classify_intent,
    classify_memory_confirmation,
    parse_mixed_utterance,
    usable_memory_facts,
)
import intent
from long_term_memory import is_valid_name, upsert_fact


# --- classify_memory_confirmation -----------------------------------------

@pytest.mark.parametrize(
    "text",
    ["yes", "Yes!", "yeah", "yep", "sure", "ok", "of course", "yes please",
     "yeah go for it"],
)
def test_confirmation_yes(text):
    assert classify_memory_confirmation(text) == "yes"


@pytest.mark.parametrize(
    "text",
    ["no", "Nope.", "nah", "no thanks", "never mind", "don't do that",
     "no way"],
)
def test_confirmation_no(text):
    assert classify_memory_confirmation(text) == "no"


@pytest.mark.parametrize(
    "text", ["maybe", "what?", "sounds good", "hey", "remember my age is 22"]
)
def test_confirmation_none(text):
    assert classify_memory_confirmation(text) is None


def test_confirmation_llm_reads_typos(monkeypatch):
    monkeypatch.setenv("INTENT_MODE", "llm")
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setattr(intent, "_llm_json", lambda prompt: {"answer": "yes"})
    assert classify_memory_confirmation("yeha") == "yes"
    monkeypatch.setattr(intent, "_llm_json", lambda prompt: {"answer": "no"})
    assert classify_memory_confirmation("naw im good") == "no"
    monkeypatch.setattr(intent, "_llm_json", lambda prompt: {"answer": "other"})
    assert classify_memory_confirmation("what's the refund policy") is None


# --- _looks_like_question ---------------------------------------------------

@pytest.mark.parametrize(
    "text",
    ["what time is it", "how old am i", "is water wet", "look this up?",
     "can you help", "Where do I live"],
)
def test_looks_like_question_true(text):
    assert _looks_like_question(text) is True


@pytest.mark.parametrize(
    "text",
    ["hey", "remember my age is 22", "I prefer shorter replies",
     "look this up", "tell me a joke"],
)
def test_looks_like_question_false(text):
    assert _looks_like_question(text) is False


# --- usable_memory_facts -----------------------------------------------------

def test_usable_facts_drops_junk_names():
    facts = [
        {"key": "name", "value": "Michelle"},
        {"key": "name", "value": "None"},
        {"key": "name", "value": "n/a"},
        {"key": "color", "value": "blue"},
        {"key": "", "value": "x"},
        {"key": "empty", "value": ""},
    ]
    assert usable_memory_facts(facts) == [{"key": "color", "value": "blue"}]


def test_usable_facts_requires_support_from_text():
    facts = [
        {"key": "favorite_color", "value": "indigo"},
        {"key": "reply_style", "value": "shorter replies"},
    ]
    kept = usable_memory_facts(facts, "please keep replies shorter")
    # "indigo" never appears in the message → dropped; "shorter"/"replies"
    # tokens do → kept.
    assert kept == [{"key": "reply_style", "value": "shorter replies"}]


def test_usable_facts_without_text_keeps_valid_facts():
    facts = [{"key": "favorite_color", "value": "indigo"}]
    assert usable_memory_facts(facts) == facts


# --- is_valid_name ------------------------------------------------------------

@pytest.mark.parametrize("value", ["Nathan", "nathan", "Mary-Jane", "O'Brien"])
def test_valid_names(value):
    assert is_valid_name(value) is True


@pytest.mark.parametrize(
    "value", [None, "", "none", "NULL", "n/a", "unknown", "Michelle",
              "michelle.ai", "user", "123", "(none)"]
)
def test_invalid_names(value):
    assert is_valid_name(value) is False


# --- upsert_fact name protection ----------------------------------------------

def test_upsert_junk_name_is_dropped():
    uid = str(uuid4())
    upsert_fact(user_id=uid, fact_key="name", fact_value="null")
    assert long_term_memory.get_fact(uid, "name") is None


def test_upsert_does_not_overwrite_name_without_replace():
    uid = str(uuid4())
    upsert_fact(user_id=uid, fact_key="name", fact_value="Nathan", replace=True)
    upsert_fact(user_id=uid, fact_key="name", fact_value="Sam", replace=False)
    assert long_term_memory.get_fact(uid, "name") == "Nathan"


def test_upsert_overwrites_name_with_replace():
    uid = str(uuid4())
    upsert_fact(user_id=uid, fact_key="name", fact_value="Nathan", replace=True)
    upsert_fact(user_id=uid, fact_key="name", fact_value="Sam", replace=True)
    assert long_term_memory.get_fact(uid, "name") == "Sam"


def test_upsert_normalizes_name_to_title_case():
    uid = str(uuid4())
    upsert_fact(user_id=uid, fact_key="name", fact_value="nathan", replace=True)
    assert long_term_memory.get_fact(uid, "name") == "Nathan"


# --- retrieve._chunk_text --------------------------------------------------------

def test_chunk_text_empty():
    assert retrieve._chunk_text("") == []
    assert retrieve._chunk_text("   \n\n  ") == []


def test_chunk_text_short_text_is_single_chunk():
    assert retrieve._chunk_text("hello world") == ["hello world"]


def test_chunk_text_merges_small_paragraphs():
    text = "first paragraph\n\nsecond paragraph"
    assert retrieve._chunk_text(text) == ["first paragraph\n\nsecond paragraph"]


def test_chunk_text_splits_long_paragraph_with_overlap():
    # One giant paragraph (no blank lines) forces the sliding-window split.
    para = "abcdefghij" * 150  # 1500 chars, CHUNK_SIZE=600, OVERLAP=80
    chunks = retrieve._chunk_text(para)
    assert len(chunks) >= 3
    assert all(len(c) <= retrieve.CHUNK_SIZE for c in chunks)
    # Consecutive chunks overlap by CHUNK_OVERLAP characters.
    assert chunks[1].startswith(chunks[0][-retrieve.CHUNK_OVERLAP:])


@pytest.mark.parametrize(
    "text",
    [
        "open Notes",
        "yo pull up chrome",
        "fire up vs code rq",
        "hop into discord",
        "pls launch xyzzyqorp",
        "can u open Notes+",
        "bruh bring up Calculator",
    ],
)
def test_slang_open_is_action_order(text):
    assert classify_intent(text)["intent"] == "ACTION"
    analysis = analyze_action_request(text)
    assert analysis["action_type"] == "open_app"
    assert analysis["resolved_params"].get("app_names")


def test_app_names_are_not_a_closed_list():
    analysis = analyze_action_request("pls launch xyzzyqorp")
    assert analysis["resolved_params"]["app_names"] == ["xyzzyqorp"]


def test_llm_empty_params_still_extracts_notes(monkeypatch):
    monkeypatch.setenv("INTENT_MODE", "llm")
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
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
    analysis = analyze_action_request("open Notes")
    assert analysis["action_type"] == "open_app"
    assert analysis["resolved_params"]["app_names"] == ["Notes"]
    assert analysis["missing_params"] == []


def test_llm_open_app_label_does_not_steal_email(monkeypatch):
    """Live bug: 'send an email to you@example.com …' came back as open_app."""
    monkeypatch.setenv("INTENT_MODE", "llm")
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
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
    analysis = analyze_action_request(
        "send an email to you@example.com subject hi body hello"
    )
    assert analysis["action_type"] == "send_email"
    assert analysis["resolved_params"]["recipient"] == "you@example.com"
    assert analysis["resolved_params"]["subject"] == "hi"
    assert analysis["resolved_params"]["body"] == "hello"
    assert analysis["missing_params"] == []


@pytest.mark.parametrize(
    "text",
    [
        "send an emauil for me",
        "send an emial for me",
        "send an emaill for me",
        "send an emal for me",
    ],
)
def test_email_typos_are_send_email_not_chat_or_open_app(text):
    """Live log: 'send an emauil for me' → open_app."""
    assert parse_mixed_utterance(text)["actions"]
    assert classify_intent(text)["intent"] == "ACTION"
    analysis = analyze_action_request(text)
    assert analysis["action_type"] == "send_email"
    assert analysis["missing_params"] == ["recipient", "subject", "body"]


def test_llm_open_app_label_does_not_steal_email_typo(monkeypatch):
    monkeypatch.setenv("INTENT_MODE", "llm")
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setattr(
        intent,
        "_analyze_action_with_llm",
        lambda *a, **k: {
            "action_type": "open_app",
            "resolved_params": {},
            "missing_params": ["app_name"],
            "related": True,
            "confidence": 0.95,
        },
    )
    analysis = analyze_action_request("send an emauil for me")
    assert analysis["action_type"] == "send_email"
    assert "app_name" not in analysis["resolved_params"]


def test_llm_chat_label_still_promotes_open_order(monkeypatch):
    """Live bug: llama3.2 returned kind=CHAT for 'open Notes'."""
    monkeypatch.setenv("INTENT_MODE", "llm")
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setattr(
        intent,
        "_classify_with_llm",
        lambda *a, **k: {
            "intent": "CHAT",
            "confidence": 0.8,
            "is_question": False,
            "kind": "CHAT",
            "memory_score": 0.0,
            "docs_score": 0.0,
            "chat_score": 0.8,
        },
    )
    result = classify_intent("open Notes")
    assert result["intent"] == "ACTION"
    assert result["kind"] == "ACTION"


@pytest.mark.parametrize(
    "text,chat,actions",
    [
        ("open Notes", "", ["open Notes"]),
        ("hey how are you, open Notes", "hey how are you", ["open Notes"]),
        (
            "whats up. also send an email to nate@example.com subject hi body hello",
            "whats up",
            ["send an email to nate@example.com subject hi body hello"],
        ),
        (
            "open Notes then send an email to alex@example.com subject hi body hello",
            "",
            [
                "open Notes",
                "send an email to alex@example.com subject hi body hello",
            ],
        ),
        (
            "send an email to alex@example.com, subject hi, body hello",
            "",
            ["send an email to alex@example.com, subject hi, body hello"],
        ),
        (
            "send another email for me",
            "",
            ["send another email for me"],
        ),
        (
            "you already sent that i want you to send another email",
            "you already sent that i want you to",
            ["send another email"],
        ),
        (
            "to alex@example.com subject Project body sorry - email feature. "
            "And this email is being said right now",
            "to alex@example.com subject Project body sorry - email feature. "
            "And this email is being said right now",
            [],
        ),
        ("send that draft", "", ["send that draft"]),
        ("finish the lunch email", "", ["finish the lunch email"]),
        ("the one about the project", "", ["the one about the project"]),
        (
            "can you show me the draft about math tutor application",
            "",
            ["can you show me the draft about math tutor application"],
        ),
        ("show me the draft about lunch", "", ["show me the draft about lunch"]),
        ("pull up the email", "", ["pull up the email"]),
        ("get me the draft", "", ["get me the draft"]),
        (
            "pull up the email about math tutor",
            "",
            ["pull up the email about math tutor"],
        ),
        (
            "can you pull up the email draft about going to the moon",
            "",
            ["can you pull up the email draft about going to the moon"],
        ),
        ("close Notes", "", ["close Notes"]),
        ("close Notes and Safari", "", ["close Notes and Safari"]),
        (
            "close Notes and quit Safari",
            "",
            ["close Notes", "quit Safari"],
        ),
        ("quit Chrome, Slack, and Notes", "", ["quit Chrome, Slack, and Notes"]),
        ("clsoe Notes", "", ["clsoe Notes"]),
        ("quti Safari", "", ["quti Safari"]),
        ("close the draft", "", ["close the draft"]),
        ("close that email", "", ["close that email"]),
    ],
)
def test_parse_mixed_utterance(text, chat, actions):
    parsed = parse_mixed_utterance(text)
    assert parsed["chat"] == chat
    assert parsed["actions"] == actions
    if actions:
        assert classify_intent(text)["intent"] == "ACTION"


def test_email_word_in_prose_is_not_an_action_order():
    text = "I'm using the email feature and this email is being said out loud."
    parsed = parse_mixed_utterance(text)
    assert parsed["actions"] == []
    assert classify_intent(text)["intent"] != "ACTION"


def test_resume_draft_is_not_a_new_send():
    assert _looks_like_resume_draft("send that draft")
    assert _looks_like_resume_draft("finish the lunch email")
    assert _looks_like_resume_draft("the one about the project")
    assert _looks_like_resume_draft(
        "can you show me the draft about math tutor application"
    )
    assert _looks_like_resume_draft("show me the draft")
    assert _looks_like_resume_draft("open the draft")
    assert _looks_like_resume_draft("pull up the email")
    assert _looks_like_resume_draft("find the draft about lunch")
    assert _looks_like_resume_draft("get me the draft")
    assert _looks_like_resume_draft("close the draft")
    assert _looks_like_resume_draft("close that email")
    assert not _looks_like_resume_draft("close Notes")
    assert not _looks_like_resume_draft("close Mail")
    assert _looks_like_resume_draft("draft about math tutor")
    assert _looks_like_resume_draft("email about the project")
    assert not _looks_like_resume_draft("send an email")
    assert not _looks_like_resume_draft("send another email for me")
    assert not _looks_like_resume_draft("show me the handbook")
    analysis = analyze_action_request("send that draft")
    assert analysis["action_type"] == "send_email"
    assert analysis["resume"] is True
    assert analysis["resolved_params"] == {}
    show = analyze_action_request(
        "can you show me the draft about math tutor application"
    )
    assert show["action_type"] == "send_email"
    assert show["resume"] is True
    assert "math tutor" in show["resume_query"]
    fresh = analyze_action_request("send an email")
    assert fresh["resume"] is not True
    assert fresh["action_type"] == "send_email"
    handbook = classify_intent("show me the handbook")
    assert handbook["intent"] != "ACTION"
    assert classify_intent(
        "can you show me the draft about math tutor application"
    )["intent"] == "ACTION"
    close_draft = analyze_action_request("close the draft")
    assert close_draft["action_type"] == "send_email"
    assert close_draft["resume"] is True
    close_email = analyze_action_request("close that email")
    assert close_email["action_type"] == "send_email"
    assert close_email["resume"] is True


def test_close_and_quit_extract_names():
    close = analyze_action_request("close Notes")
    assert close["action_type"] == "close_app"
    assert close["resolved_params"]["app_names"] == ["Notes"]
    assert close["missing_params"] == []
    multi = analyze_action_request("close Notes and Safari")
    assert multi["action_type"] == "close_app"
    assert multi["resolved_params"]["app_names"] == ["Notes", "Safari"]
    oxford = analyze_action_request("quit Chrome, Slack, and Notes")
    assert oxford["action_type"] == "quit_app"
    assert oxford["resolved_params"]["app_names"] == ["Chrome", "Slack", "Notes"]
    typo = analyze_action_request("clsoe Notes")
    assert typo["action_type"] == "close_app"
    qtyp = analyze_action_request("quti Safari")
    assert qtyp["action_type"] == "quit_app"
    mail = analyze_action_request("close Mail")
    assert mail["action_type"] == "close_app"
    assert mail["resolved_params"]["app_names"] == ["Mail"]
    assert classify_intent("that's quite nice")["intent"] != "ACTION"
    mixed = parse_mixed_utterance("open music and quit messages")
    assert mixed["actions"] == ["open music", "quit messages"]
    quit_clause = analyze_action_request("quit messages")
    assert quit_clause["action_type"] == "quit_app"
    assert quit_clause["resolved_params"]["app_names"] == ["messages"]
    assert quit_clause["missing_params"] == []
    both = analyze_action_request("open Notes and Safari")
    assert both["action_type"] == "open_app"
    assert both["resolved_params"]["app_names"] == ["Notes", "Safari"]
    oxford_open = analyze_action_request("open Chrome, Slack, and Notes")
    assert oxford_open["resolved_params"]["app_names"] == ["Chrome", "Slack", "Notes"]


def test_llm_empty_quit_names_do_not_stick(monkeypatch):
    monkeypatch.setenv("INTENT_MODE", "llm")
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    called = []

    def boom(*a, **k):
        called.append(True)
        return {
            "action_type": "quit_app",
            "resolved_params": {},
            "missing_params": ["app_names"],
            "related": True,
            "confidence": 0.9,
        }

    monkeypatch.setattr(intent, "_analyze_action_with_llm", boom)
    analysis = analyze_action_request("quit messages")
    assert analysis["action_type"] == "quit_app"
    assert analysis["resolved_params"]["app_names"] == ["messages"]
    assert analysis["missing_params"] == []
    assert called == []


def test_fuzzy_app_name_prefers_running_and_skips_near_misses(monkeypatch):
    monkeypatch.setattr(
        actions, "_list_running_app_names", lambda: ["Notes", "Safari", "Notion"]
    )
    monkeypatch.setattr(actions, "_list_installed_app_names", lambda: [])
    assert actions.resolve_app_name("Safar") == "Safari"
    assert actions.resolve_app_name("Notes") == "Notes"
    assert actions.resolve_app_name("Zorbulon") is None


def test_notes_does_not_resolve_to_notes_plus_when_notes_already_quit(monkeypatch):
    monkeypatch.setattr(actions, "_list_running_app_names", lambda: ["Notes+", "Safari"])
    monkeypatch.setattr(
        actions, "_list_installed_app_names", lambda: ["Notes", "Notes+", "Safari"]
    )
    assert actions.resolve_app_name("Notes") is None
    assert actions.resolve_app_target("Notes") == ("not_running", "Notes")
    assert actions.resolve_app_target("Notes+") == ("running", "Notes+")
    assert actions._fuzzy_pick("Notes", ["Notes+"]) is None


def test_llm_close_label_recovers_from_rules(monkeypatch):
    monkeypatch.setenv("INTENT_MODE", "llm")
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
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
    analysis = analyze_action_request("close Notes and Safari")
    assert analysis["action_type"] == "close_app"
    assert analysis["resolved_params"]["app_names"] == ["Notes", "Safari"]
    assert analysis["missing_params"] == []


def test_resume_draft_promotes_retrieve_to_action(monkeypatch):
    monkeypatch.setenv("INTENT_MODE", "rules")
    monkeypatch.setattr(
        intent,
        "_classify_with_rules",
        lambda text: {"intent": "RETRIEVE", "confidence": 0.75},
    )
    text = "can you show me the draft about math tutor application"
    out = classify_intent(text)
    assert out["intent"] == "ACTION"
    assert out["kind"] == "ACTION"
    handbook = classify_intent("show me the handbook")
    assert handbook["intent"] == "RETRIEVE"


def test_resume_draft_promotes_remember_to_action(monkeypatch):
    monkeypatch.setenv("INTENT_MODE", "llm")
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setattr(
        intent,
        "_classify_with_llm",
        lambda *a, **k: {
            "intent": "REMEMBER",
            "kind": "REMEMBER",
            "confidence": 0.8,
            "is_question": True,
        },
    )
    text = "Can you show me the draft about math tutor application"
    out = classify_intent(text)
    assert out["intent"] == "ACTION"
    assert out["kind"] == "ACTION"


def test_pull_up_the_email_is_resume_not_open_app(monkeypatch):
    text = "pull up the email about math tutor"
    parsed = parse_mixed_utterance(text)
    assert parsed["actions"] == [text]
    rules = analyze_action_request(text)
    assert rules["action_type"] == "send_email"
    assert rules["resume"] is True
    assert "app_name" not in rules["resolved_params"]
    monkeypatch.setenv("INTENT_MODE", "llm")
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setattr(
        intent,
        "_analyze_action_with_llm",
        lambda *a, **k: {
            "action_type": "open_app",
            "resolved_params": {"app_name": "the"},
            "missing_params": ["app_name"],
            "related": True,
            "confidence": 0.9,
        },
    )
    out = analyze_action_request(text)
    assert out["action_type"] == "send_email"
    assert out["resume"] is True
    assert out["resolved_params"] == {}


def test_string_resolved_params_does_not_crash_analyze(monkeypatch):
    monkeypatch.setenv("INTENT_MODE", "llm")
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setattr(
        intent,
        "_analyze_action_with_llm",
        lambda *a, **k: {
            "action_type": "send_email",
            "resolved_params": "send that draft",
            "missing_params": [],
            "related": True,
            "confidence": 0.9,
        },
    )
    out = analyze_action_request("send that draft")
    assert out["action_type"] == "send_email"
    assert out["resume"] is True
    assert out["resolved_params"] == {}


def _gmail_b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def test_extract_draft_fields_gmail_mime_parts():
    plain = "plain body from parts"
    raw = {
        "payload": {
            "headers": [
                {"name": "To", "value": "pat@example.com"},
                {"name": "Subject", "value": "hello"},
            ],
            "mimeType": "multipart/alternative",
            "body": {"size": 0},
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _gmail_b64(plain)}},
                {
                    "mimeType": "text/html",
                    "body": {"data": _gmail_b64("<p>html should lose</p>")},
                },
            ],
        }
    }
    fields = actions._extract_draft_fields(raw)
    assert fields["recipient"] == "pat@example.com"
    assert fields["subject"] == "hello"
    assert fields["body"] == plain


def test_extract_draft_fields_gmail_html_only_parts():
    fields = actions._extract_draft_fields(
        {
            "payload": {
                "body": {"size": 0},
                "parts": [
                    {
                        "mimeType": "text/html",
                        "body": {"data": _gmail_b64("<p>hello <b>there</b></p>")},
                    }
                ],
            }
        }
    )
    assert fields["body"] == "hello there"


def test_extract_draft_fields_outlook_body_content():
    fields = actions._extract_draft_fields({"body": {"content": "graph body"}})
    assert fields["body"] == "graph body"
    unique = actions._extract_draft_fields(
        {"uniqueBody": {"content": "unique graph"}}
    )
    assert unique["body"] == "unique graph"


def test_extract_draft_fields_parses_stringified_data():
    import json

    plain = "stringified wrapper body"
    inner = {
        "payload": {
            "headers": [
                {"name": "To", "value": "pat@example.com"},
                {"name": "Subject", "value": "hello"},
            ],
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _gmail_b64(plain)}},
            ],
        }
    }
    fields = actions._extract_draft_fields({"data": json.dumps(inner)})
    assert fields["body"] == plain
    double = actions._extract_draft_fields({"data": json.dumps(json.dumps(inner))})
    assert double["body"] == plain


def test_extract_draft_fields_rfc2822_raw():
    body = "math tutor application body"
    rfc = (
        "To: hire@example.com\r\n"
        "Subject: FYI\r\n"
        "Content-Type: text/plain; charset=UTF-8\r\n"
        "\r\n"
        f"{body}\r\n"
    )
    fields = actions._extract_draft_fields(
        {
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
            },
            "raw": _gmail_b64(rfc),
        }
    )
    assert fields["body"].strip() == body
    assert fields["recipient"] == "hire@example.com"


def test_match_draft_uses_snippet():
    drafts = [
        {
            "provider": "gmail",
            "remote_id": "s",
            "recipient": "a@example.com",
            "subject": "hello",
            "body": "",
            "snippet": "math tutor application",
        }
    ]
    out = actions.match_draft(
        "the draft about math tutor application", drafts, resume=True
    )
    assert out["status"] == "hit"
    assert out["draft"]["remote_id"] == "s"


def test_new_email_does_not_reuse_last_send_from_history(monkeypatch):
    """Reload keeps the old send in the thread. A bare 'send another' must
    not Confirm that same mail again (live log: last Gmail reused)."""
    monkeypatch.setenv("INTENT_MODE", "llm")
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setattr(
        intent,
        "_analyze_action_with_llm",
        lambda *a, **k: {
            "action_type": "send_email",
            "resolved_params": {
                "recipient": "nathan.ze.zhao@gmail.com",
                "subject": "[urgent]",
                "body": "Hey Nathan, we are behind schedule.",
            },
            "missing_params": [],
            "related": True,
            "confidence": 0.9,
        },
    )
    history = [
        {
            "role": "user",
            "content": (
                "to nathan.ze.zhao@gmail.com subject [urgent] body "
                "Hey Nathan, we are behind schedule."
            ),
        },
        {
            "role": "assistant",
            "content": "Sent the email to nathan.ze.zhao@gmail.com.",
        },
    ]
    out = analyze_action_request("send another email for me", history)
    assert out["action_type"] == "send_email"
    assert out["resolved_params"] == {}
    assert set(out["missing_params"]) == {"recipient", "subject", "body"}


@pytest.mark.parametrize(
    "text",
    ["nevermind", "never mind", "nvm", "forget it", "scratch that", "don't send"],
)
def test_composer_dismiss_phrases(text):
    assert _looks_like_composer_dismiss(text)
    out = analyze_action_request(
        text,
        task_context={
            "action_type": "send_email",
            "resolved_params": {},
            "missing_params": ["recipient", "subject", "body"],
        },
    )
    assert out["dismiss"] is True
    assert out["related"] is False


def test_composer_dismiss_llm_similar_phrase(monkeypatch):
    monkeypatch.setenv("INTENT_MODE", "llm")
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setattr(
        intent,
        "_llm_json",
        lambda prompt: {"dismiss": True},
    )
    assert intent.classify_composer_dismiss("yeah I'm good on that email")


def test_composer_followup_is_not_dismiss():
    out = analyze_action_request(
        "subject hi body hello",
        task_context={
            "action_type": "send_email",
            "resolved_params": {"recipient": "alex"},
            "missing_params": ["subject", "body"],
        },
    )
    assert out["dismiss"] is False
    assert out["related"] is True


def test_polish_email_body_mock_keeps_words():
    import llm

    spoken = "Hi my names Nathan thanks for lunch"
    assert llm.polish_email_body(spoken) == spoken


def test_polish_email_body_ollama_is_not_michelle_and_skips_memory(monkeypatch):
    import llm

    captured = {}

    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"message": {"content": "Hi, my name is Nathan."}}

    def fake_post(url, json=None, timeout=None):
        captured["json"] = json
        return FakeResp()

    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setattr(llm.httpx, "post", fake_post)
    out = llm.polish_email_body("Hi my names Nathan")
    assert out == "Hi, my name is Nathan."
    messages = captured["json"]["messages"]
    system = messages[0]["content"]
    assert messages[0]["role"] == "system"
    assert "You are Michelle" not in system
    assert "grammar editor" in system
    assert len(messages) == 2
    assert captured["json"]["options"]["temperature"] == 0.0
    assert "Hi my names Nathan" in messages[1]["content"]


def test_match_draft_generic_picks_newest_not_all_fifty():
    drafts = [
        {
            "provider": "gmail",
            "remote_id": "newest",
            "recipient": "a@example.com",
            "subject": "alpha",
            "body": "",
        },
        {
            "provider": "gmail",
            "remote_id": "older",
            "recipient": "b@example.com",
            "subject": "beta",
            "body": "",
        },
    ]
    out = actions.match_draft("send that draft", drafts, resume=True)
    assert out["status"] == "newest"
    assert out["draft"]["remote_id"] == "newest"
    alias = actions.match_gmail_draft("send that draft", drafts, resume=True)
    assert alias == out


def test_match_draft_filler_does_not_tie_on_you():
    drafts = [
        {
            "provider": "gmail",
            "remote_id": "a",
            "recipient": "a@example.com",
            "subject": "[urgent]",
            "body": "can you send this when you can",
        },
        {
            "provider": "gmail",
            "remote_id": "b",
            "recipient": "b@example.com",
            "subject": "[urgent]",
            "body": "thank you for waiting",
        },
    ]
    out = actions.match_draft("can you pull up the email", drafts, resume=True)
    assert out["status"] == "newest"
    assert out["draft"]["remote_id"] == "a"


def test_match_draft_moon_beats_empty_urgent():
    drafts = [
        {
            "provider": "gmail",
            "remote_id": "u1",
            "recipient": "nate@example.com",
            "subject": "[urgent]",
            "body": "",
        },
        {
            "provider": "gmail",
            "remote_id": "moon",
            "recipient": "sd@nothing.com",
            "subject": "trip",
            "body": "going to the moon next week",
        },
        {
            "provider": "gmail",
            "remote_id": "u2",
            "recipient": "nate@example.com",
            "subject": "[urgent]",
            "body": "",
        },
    ]
    out = actions.match_draft(
        "can you pull up the email draft about going to the moon",
        drafts,
        resume=True,
    )
    assert out["status"] == "hit"
    assert out["draft"]["remote_id"] == "moon"


def test_pick_draft_llm_only_uses_listed_ids(monkeypatch):
    monkeypatch.setenv("INTENT_MODE", "llm")
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    drafts = [
        {
            "provider": "gmail",
            "remote_id": "moon",
            "recipient": "sd@nothing.com",
            "subject": "trip",
            "body": "going to the moon",
        },
        {
            "provider": "gmail",
            "remote_id": "u1",
            "recipient": "nate@example.com",
            "subject": "[urgent]",
            "body": "please review",
        },
    ]
    monkeypatch.setattr(
        actions,
        "_llm_pick_ids",
        lambda utterance, shelf: ["moon"],
    )
    out = actions.pick_draft(
        "the one about going to the moon", drafts, resume=True
    )
    assert out["status"] == "hit"
    assert out["draft"]["remote_id"] == "moon"
    monkeypatch.setattr(
        actions,
        "_llm_pick_ids",
        lambda utterance, shelf: ["invented"],
    )
    out = actions.pick_draft("the one about going to the moon", drafts, resume=True)
    assert out["status"] == "miss"
    drafts = [
        {
            "provider": "gmail",
            "remote_id": "a",
            "recipient": "alex@example.com",
            "subject": "lunch",
            "body": "",
        },
        {
            "provider": "gmail",
            "remote_id": "b",
            "recipient": "sam@example.com",
            "subject": "lunch plans",
            "body": "",
        },
    ]
    out = actions.match_draft("the one about lunch", drafts, resume=True)
    assert out["status"] == "ambiguous"
    assert len(out["candidates"]) == 2
