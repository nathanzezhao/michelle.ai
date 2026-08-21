"""Suite 6 — doc retrieval hit and miss (rules mode, mock context answers)."""

from conftest import REFUND_MARKER, chat
from llm import RETRIEVE_MISS_REPLY


def test_refund_question_hits_doc(client, ids):
    body = chat(client, "what is the refund policy", ids)
    assert body["intent"] == "RETRIEVE"
    assert body["is_question"] is True
    assert body["sources"] == ["refund_policy.md"]
    # Mock context answer excerpts the matching chunk.
    assert body["answer"].startswith("From refund_policy.md:")
    assert REFUND_MARKER in body["answer"]


def test_unknown_topic_misses(client, ids):
    body = chat(client, "what is the teleportation policy", ids)
    assert body["intent"] == "RETRIEVE"
    assert body["sources"] == []
    assert body["answer"] == RETRIEVE_MISS_REPLY


def test_chat_after_miss_is_not_stuck_on_topic(client, ids):
    chat(client, "what is the teleportation policy", ids)
    body = chat(client, "hey", ids)
    assert body["intent"] == "CHAT"
    assert (
        body["answer"]
        == "Hi! I'm Michelle in mock mode. No API credits are being used."
    )


def test_sources_key_only_present_on_retrieve_turns(client, ids):
    hit = chat(client, "what is the refund policy", ids)
    assert "sources" in hit
    plain = chat(client, "hey", ids)
    # Frozen contract: non-RETRIEVE payloads omit the sources key entirely.
    assert "sources" not in plain
