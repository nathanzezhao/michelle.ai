"""Saved chatlog API — the window does not paint this on open."""

from memory import save_message


def test_session_history_returns_messages(client, ids):
    cid = ids["conversation_id"]
    uid = ids["user_id"]
    save_message(cid, "user", "hello")
    save_message(cid, "assistant", "hi back")

    r = client.get(
        "/session/history",
        params={"conversation_id": cid, "user_id": uid},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["conversation_id"] == cid
    assert body["user_id"] == uid
    assert len(body["messages"]) >= 2
    assert body["messages"][-2]["role"] == "user"
    assert body["messages"][-2]["content"] == "hello"
