"""Suite 1 — /chat input guardrails (blank, oversized, bad IDs)."""

from uuid import UUID

import main
from conftest import chat


def _is_uuid(value: str) -> bool:
    try:
        UUID(value)
        return True
    except ValueError:
        return False


def test_blank_message_rejected(client, ids):
    body = chat(client, "   ", ids)
    assert body["answer"] == "Please type a message first."
    assert body["conversation_id"] == ids["conversation_id"]
    assert body["user_id"] == ids["user_id"]
    # Frozen behavior: guardrail responses carry NO intent/remembered/
    # asked_to_remember keys — the payload is answer + ids only.
    assert "intent" not in body
    assert "remembered" not in body


def test_too_long_message_rejected(client, ids):
    n = main.MAX_MESSAGE_CHARS + 1
    body = chat(client, "x" * n, ids)
    assert body["answer"] == (
        f"That message is too long ({n} characters). "
        f"Please keep it under {main.MAX_MESSAGE_CHARS} characters."
    )
    assert "intent" not in body


def test_exactly_max_length_accepted(client, ids):
    # Boundary: len == MAX_MESSAGE_CHARS passes the guardrail.
    body = chat(client, "x" * main.MAX_MESSAGE_CHARS, ids)
    assert "too long" not in body["answer"]
    assert body["intent"] == "CHAT"


def test_garbage_ids_replaced_with_fresh_uuids(client):
    resp = client.post(
        "/chat",
        json={
            "text": "hey",
            "conversation_id": "not-a-uuid",
            "user_id": "also garbage!!",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["conversation_id"] != "not-a-uuid"
    assert body["user_id"] != "also garbage!!"
    assert _is_uuid(body["conversation_id"])
    assert _is_uuid(body["user_id"])


def test_missing_ids_generate_fresh_uuids(client):
    body = client.post("/chat", json={"text": "hey"}).json()
    assert _is_uuid(body["conversation_id"])
    assert _is_uuid(body["user_id"])


def test_valid_uuid_passes_through_unchanged(client, ids):
    body = chat(client, "hey", ids)
    assert body["conversation_id"] == ids["conversation_id"]
    assert body["user_id"] == ids["user_id"]
