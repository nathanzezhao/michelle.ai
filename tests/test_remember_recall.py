"""Suite 4 — explicit remember store, then recall (rules + mock)."""

import long_term_memory
from conftest import chat


def test_remember_age_saves_fact(client, ids):
    body = chat(client, "remember this: my age is 22", ids)
    assert body["intent"] == "REMEMBER"
    assert body["is_question"] is False
    assert body["remembered"] == [
        {"key": "age", "value": "22", "priority": "high"}
    ]
    assert long_term_memory.get_fact(ids["user_id"], "age") == "22"
    # Save-confirmation goes through the mock provider with the internal
    # system tag appended (frozen as-is).
    assert "age: 22" in body["answer"]


def test_recall_age_answers_from_fact(client, ids):
    chat(client, "remember this: my age is 22", ids)
    body = chat(client, "how old am i", ids)
    assert body["intent"] == "REMEMBER"
    assert body["is_question"] is True
    # Mock provider answers straight from the saved fact.
    assert body["answer"] == "22"

    # FROZEN ODDITY: the rules remember-analyzer labels "how old am i" as a
    # STORE (its question regex doesn't match this phrasing), so the recall
    # turn also writes a junk `note: how old am i` fact as a side effect.
    assert body["remembered"] == [
        {"key": "note", "value": "how old am i", "priority": "high"}
    ]
    assert (
        long_term_memory.get_fact(ids["user_id"], "note") == "how old am i"
    )


def test_recall_age_without_fact_gives_mock_fallback(client, ids):
    body = chat(client, "how old am i", ids)
    assert body["intent"] == "REMEMBER"
    assert "I don't have your age saved yet" in body["answer"]


def test_keep_in_mind_phrasing_is_remember(client, ids):
    body = chat(client, "keep in mind that my dog is named Waffles", ids)
    assert body["intent"] == "REMEMBER"
    # FROZEN ODDITY: after "keep in mind" is stripped, the "X is named Y"
    # extractor keeps the leading "that my", producing key `that_my_dog`
    # instead of `dog`. Value is captured correctly.
    assert (
        long_term_memory.get_fact(ids["user_id"], "that_my_dog") == "Waffles"
    )
    assert body["remembered"] == [
        {"key": "that_my_dog", "value": "Waffles", "priority": "high"}
    ]
