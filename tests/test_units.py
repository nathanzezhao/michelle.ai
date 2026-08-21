"""Suite 8 — unit tests on the deterministic seams (no HTTP)."""

from uuid import uuid4

import pytest

import long_term_memory
import retrieve
from intent import (
    _looks_like_question,
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
    assert analysis["resolved_params"].get("app_name")


def test_app_names_are_not_a_closed_list():
    analysis = analyze_action_request("pls launch xyzzyqorp")
    assert analysis["resolved_params"]["app_name"] == "xyzzyqorp"


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
    assert analysis["resolved_params"]["app_name"] == "Notes"
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
    ],
)
def test_parse_mixed_utterance(text, chat, actions):
    parsed = parse_mixed_utterance(text)
    assert parsed["chat"] == chat
    assert parsed["actions"] == actions
    assert classify_intent(text)["intent"] == "ACTION"
