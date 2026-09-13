"""Suite 2 — /session/start greeting + history side effects."""

from uuid import uuid4

import long_term_memory
import main
from memory import get_history, save_message
import session_context


def test_fresh_session_asks_for_name(client):
    body = client.post("/session/start", json={}).json()
    assert body["greeting"] == main.NAME_PROMPT
    assert body["ask_name"] is True
    assert body["name"] is None
    # The name ask is persisted as an assistant row so a bare "Nathan"
    # reply can be understood as the name.
    history = get_history(body["conversation_id"])
    assert history == [
        {"role": "assistant", "content": main.NAME_PROMPT, "kind": None}
    ]


def test_second_session_start_does_not_duplicate_prompt_row(client):
    first = client.post("/session/start", json={}).json()
    cid, uid = first["conversation_id"], first["user_id"]
    second = client.post(
        "/session/start", json={"conversation_id": cid, "user_id": uid}
    ).json()
    assert second["greeting"] == main.NAME_PROMPT
    history = get_history(cid)
    assert len(history) == 1  # still exactly one assistant prompt row


def test_session_start_with_known_name_personalizes(client):
    uid = str(uuid4())
    long_term_memory.upsert_fact(
        user_id=uid, fact_key="name", fact_value="Nathan", replace=True
    )
    body = client.post(
        "/session/start", json={"conversation_id": None, "user_id": uid}
    ).json()
    assert body["greeting"] == "Hey Nathan, what's up?"
    assert body["ask_name"] is False
    assert body["name"] == "Nathan"
    # Returning users get no new assistant row in history.
    assert get_history(body["conversation_id"]) == []


def test_session_start_replaces_garbage_ids(client):
    body = client.post(
        "/session/start",
        json={"conversation_id": "junk", "user_id": "more junk"},
    ).json()
    assert body["conversation_id"] != "junk"
    assert body["user_id"] != "more junk"


def test_session_start_clears_working_pad_keeps_chatlog_and_facts(client, ids):
    uid, cid = ids["user_id"], ids["conversation_id"]
    long_term_memory.upsert_fact(user_id=uid, fact_key="name", fact_value="Nathan", replace=True)
    save_message(cid, "user", "hello from last time")
    save_message(cid, "assistant", "hey")
    session_context.record_action(uid, cid, "open_app", app_names=["Notes"])
    assert session_context.get(uid, cid)["last_opened"] == ["Notes"]

    body = client.post("/session/start", json=ids).json()
    pad = session_context.get(uid, cid)
    assert pad["last_opened"] == []
    assert pad["last_action_type"] is None
    assert body["greeting"] == "Hey Nathan, what's up?"
    assert long_term_memory.get_fact(uid, "name") == "Nathan"
    history = get_history(cid)
    assert any(m["content"] == "hello from last time" for m in history)


def test_clear_all_empties_pads_not_facts():
    uid, cid = str(uuid4()), str(uuid4())
    session_context.init_db()
    long_term_memory.init_db()
    session_context.record_action(uid, cid, "open_app", app_names=["Notes"])
    long_term_memory.upsert_fact(user_id=uid, fact_key="color", fact_value="indigo", replace=True)
    session_context.clear_all()
    assert session_context.get(uid, cid)["last_opened"] == []
    assert long_term_memory.get_fact(uid, "color") == "indigo"
