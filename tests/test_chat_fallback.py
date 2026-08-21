"""Suite 7 — plain chat fallback, full JSON contract, and INTENT_MODE=mock."""

from conftest import chat


def test_hey_is_chat_with_mock_reply(client, ids):
    body = chat(client, "hey", ids)
    assert body["intent"] == "CHAT"
    assert (
        body["answer"]
        == "Hi! I'm Michelle in mock mode. No API credits are being used."
    )


def test_chat_payload_contract(client, ids):
    """Freeze the exact key set of a normal /chat response."""
    body = chat(client, "hey", ids)
    assert set(body.keys()) == {
        "answer",
        "conversation_id",
        "user_id",
        "intent",
        "is_question",
        "kind",
        "memory_score",
        "docs_score",
        "chat_score",
        "remembered",
        "asked_to_remember",
        # SPEC-PIPELINE §8: "engine" is present on EVERY turn (lowercase),
        # non-action turns carry no task fields.
        "engine",
    }
    assert body["engine"] == "chat"
    assert body["is_question"] is False
    assert body["kind"] == "CHAT"
    assert body["remembered"] == []
    assert body["asked_to_remember"] is False
    assert isinstance(body["memory_score"], float)


def test_generic_statement_falls_back_to_chat(client, ids):
    body = chat(client, "the weather was nice today", ids)
    assert body["intent"] == "CHAT"
    assert body["answer"].startswith("[mock mode] I heard:")


def test_intent_mode_mock_greeting_is_chat(client, ids, monkeypatch):
    monkeypatch.setenv("INTENT_MODE", "mock")
    body = chat(client, "hey", ids)
    assert body["intent"] == "CHAT"


def test_intent_mode_mock_question_routes_to_retrieve(client, ids, monkeypatch):
    # Frozen: the mock classifier sends ANY "?" message to RETRIEVE.
    monkeypatch.setenv("INTENT_MODE", "mock")
    body = chat(client, "is water wet?", ids)
    assert body["intent"] == "RETRIEVE"
    assert body["sources"] == []


def test_intent_mode_mock_remember_command(client, ids, monkeypatch):
    monkeypatch.setenv("INTENT_MODE", "mock")
    body = chat(client, "remember this: my age is 31", ids)
    assert body["intent"] == "REMEMBER"
    assert body["remembered"] == [
        {"key": "age", "value": "31", "priority": "high"}
    ]
