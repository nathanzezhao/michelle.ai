"""Suite 5 — pending-memory yes/no confirmation flow.

FROZEN ODDITY (R0 documents, does not fix): the /chat "want me to remember
that?" ask path is currently UNREACHABLE. maybe_promote_to_remember fires on
the exact same conditions (ask_user/important + facts grounded in the text)
before the CHAT branch can set a pending memory, so ask-worthy messages get
promoted to REMEMBER and stored immediately, and asked_to_remember is never
True. The first test freezes that. The yes/no handler itself IS reachable
whenever a pending row exists in the DB, so the rest of the suite seeds one
via long_term_memory.set_pending_memory to freeze its behavior.
"""

import long_term_memory
from conftest import chat

PENDING_FACTS = [
    {"key": "reply_style", "value": "shorter replies", "priority": "medium"}
]


def _seed_pending(ids, facts=PENDING_FACTS):
    long_term_memory.set_pending_memory(
        ids["user_id"], ids["conversation_id"], facts
    )


def test_preference_is_promoted_not_asked(client, ids):
    body = chat(client, "I prefer shorter replies", ids)
    # Expected-by-design would be asked_to_remember=True; current reality is
    # immediate promotion + a junk note store. Freeze reality.
    assert body["asked_to_remember"] is False
    assert body["intent"] == "REMEMBER"
    assert body["remembered"] == [
        {"key": "note", "value": "I prefer shorter replies", "priority": "high"}
    ]
    assert (
        long_term_memory.get_pending_memory(
            ids["user_id"], ids["conversation_id"]
        )
        == []
    )


def test_pending_yes_saves_fact(client, ids):
    _seed_pending(ids)
    body = chat(client, "yes", ids)
    assert body["answer"] == (
        "Cool — saved for future conversations (reply_style: shorter replies)."
    )
    assert body["intent"] == "REMEMBER"
    assert body["remembered"] == PENDING_FACTS
    # Frozen: the confirmed save hardcodes priority='high' in the DB even
    # though the pending fact (and the response payload) said medium.
    facts = long_term_memory.get_facts(ids["user_id"])
    assert facts == [
        {
            "key": "reply_style",
            "value": "shorter replies",
            "confidence": 1.0,
            "priority": "high",
        }
    ]
    assert (
        long_term_memory.get_pending_memory(
            ids["user_id"], ids["conversation_id"]
        )
        == []
    )


def test_pending_no_discards_fact(client, ids):
    _seed_pending(ids)
    body = chat(client, "no", ids)
    assert body["answer"] == "No worries — I won't save that."
    assert body["intent"] == "CHAT"
    assert body["remembered"] == []
    assert long_term_memory.get_facts(ids["user_id"]) == []
    assert (
        long_term_memory.get_pending_memory(
            ids["user_id"], ids["conversation_id"]
        )
        == []
    )


def test_pending_unrelated_message_drops_silently(client, ids):
    _seed_pending(ids)
    body = chat(client, "hey", ids)
    # Normal chat reply, no mention of the pending ask, pending cleared.
    assert body["intent"] == "CHAT"
    assert (
        body["answer"]
        == "Hi! I'm Michelle in mock mode. No API credits are being used."
    )
    assert long_term_memory.get_facts(ids["user_id"]) == []
    assert (
        long_term_memory.get_pending_memory(
            ids["user_id"], ids["conversation_id"]
        )
        == []
    )


def test_pending_yes_with_only_junk_facts_saves_nothing(client, ids):
    # usable_memory_facts drops name=Michelle, leaving nothing to save.
    _seed_pending(ids, facts=[{"key": "name", "value": "Michelle"}])
    body = chat(client, "yes", ids)
    assert body["answer"] == "No worries — nothing to save there."
    assert body["intent"] == "CHAT"
    assert body["remembered"] == []
    assert long_term_memory.get_facts(ids["user_id"]) == []
