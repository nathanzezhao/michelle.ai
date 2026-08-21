"""Suite 3 — name capture after the session/start prompt (rules + mock)."""

import long_term_memory
from conftest import chat


def _facts(uid):
    return {f["key"]: f["value"] for f in long_term_memory.get_facts(uid)}


def test_bare_name_reply_saves_name_fact(client):
    session = client.post("/session/start", json={}).json()
    ids = {
        "conversation_id": session["conversation_id"],
        "user_id": session["user_id"],
    }
    body = chat(client, "Nathan", ids)

    facts = _facts(ids["user_id"])
    assert facts["name"] == "Nathan"

    # FROZEN ODDITY (R0 documents, does not fix): the assessor promotes the
    # bare-name turn to REMEMBER, whose rules analyzer ALSO stores the raw
    # text as a junk `note: Nathan` fact, and the mock answer leaks the
    # internal "[System: You just saved..." prompt tag.
    assert body["intent"] == "REMEMBER"
    assert facts.get("note") == "Nathan"
    assert body["remembered"] == [
        {"key": "note", "value": "Nathan", "priority": "high"}
    ]
    assert "[System: You just saved these long-term facts" in body["answer"]

    # FROZEN ODDITY: the capture_introduced_name early-return in
    # classify_intent skips score normalization, so the score fields are
    # null on this turn (every other turn has floats).
    assert body["memory_score"] is None
    assert body["docs_score"] is None
    assert body["chat_score"] is None


def test_relaunch_greets_with_saved_name(client):
    session = client.post("/session/start", json={}).json()
    ids = {
        "conversation_id": session["conversation_id"],
        "user_id": session["user_id"],
    }
    chat(client, "Nathan", ids)
    relaunch = client.post(
        "/session/start",
        json={"conversation_id": None, "user_id": ids["user_id"]},
    ).json()
    assert relaunch["greeting"] == "Hey Nathan, what's up?"
    assert relaunch["ask_name"] is False
    assert relaunch["name"] == "Nathan"


def test_junk_name_none_is_rejected(client):
    session = client.post("/session/start", json={}).json()
    ids = {
        "conversation_id": session["conversation_id"],
        "user_id": session["user_id"],
    }
    body = chat(client, "none", ids)
    assert body["intent"] == "CHAT"
    assert long_term_memory.get_fact(ids["user_id"], "name") is None
    assert long_term_memory.get_facts(ids["user_id"]) == []


def test_junk_name_michelle_is_rejected(client):
    session = client.post("/session/start", json={}).json()
    ids = {
        "conversation_id": session["conversation_id"],
        "user_id": session["user_id"],
    }
    body = chat(client, "Michelle", ids)
    assert body["intent"] == "CHAT"
    assert long_term_memory.get_fact(ids["user_id"], "name") is None


def test_my_name_is_form_saves_name(client):
    session = client.post("/session/start", json={}).json()
    ids = {
        "conversation_id": session["conversation_id"],
        "user_id": session["user_id"],
    }
    chat(client, "my name is nathan", ids)
    # Names are canonicalized to title case on save.
    assert long_term_memory.get_fact(ids["user_id"], "name") == "Nathan"
