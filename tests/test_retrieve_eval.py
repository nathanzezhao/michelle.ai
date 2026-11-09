"""Retrieve evaluator + one close-miss diagnostic (Ada, tests first).

LLM_PROVIDER=mock / INTENT_MODE=rules from conftest. No live Ollama.
"""

from __future__ import annotations

import types

import pytest

import actions
import retrieve
import session_context
from conftest import REFUND_MARKER, chat
from llm import RETRIEVE_MISS_REPLY


TELEPORT = "what is the teleportation policy"


def native_opens(fake):
    return [argv for argv in fake.calls if argv[:2] == ["open", "-a"]]


def _gap(ids):
    return session_context.get(ids["user_id"], ids["conversation_id"]).get(
        "last_retrieve_gap"
    )


def _spy_grounded(monkeypatch):
    calls = []

    def _capture(prompt, context, history=None, long_term_facts=None, session_context=None):
        calls.append({"prompt": prompt, "context": context, "history": history})
        return "LEAK-FROM-LLM"

    monkeypatch.setattr("main.ask_llm_with_context", _capture)
    return calls


@pytest.fixture
def fake_open(monkeypatch):
    class FakeRun:
        def __init__(self):
            self.calls = []

        def run(self, argv, **kwargs):
            assert isinstance(argv, list)
            assert not kwargs.get("shell")
            self.calls.append(argv)
            listing = argv and argv[0] == "osascript" and any(
                "every process" in str(part) for part in argv
            )
            if listing:
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    fake = FakeRun()
    monkeypatch.setattr(actions.subprocess, "run", fake.run)
    monkeypatch.setattr(actions, "_list_installed_app_names", lambda: [])
    return fake


def test_evaluate_chunks_empty_is_miss():
    assert retrieve._relevant_chunks("teleportation policy", []) == []


def test_evaluate_chunks_empty_tokens_is_miss():
    chunks = [{"source": "refund_policy.md", "content": "Our refund policy"}]
    assert retrieve._relevant_chunks("???", chunks) == []
    assert retrieve._relevant_chunks("", chunks) == []


def test_evaluate_chunks_keeps_refund_hit():
    chunks = [
        {
            "source": "refund_policy.md",
            "content": f"Our refund policy. Code {REFUND_MARKER}.",
        }
    ]
    kept = retrieve._relevant_chunks("what is the refund policy", chunks)
    assert kept == chunks
    assert REFUND_MARKER in kept[0]["content"]


def test_evaluate_chunks_weak_policy_overlap_is_miss():
    chunks = [
        {
            "source": "refund_policy.md",
            "content": f"Our refund policy. Code {REFUND_MARKER}.",
        }
    ]
    assert retrieve._relevant_chunks("what is the teleportation policy", chunks) == []


def test_hopeless_search_empty_is_verbatim(client, ids, monkeypatch):
    monkeypatch.setattr(retrieve, "search", lambda query, limit=5: [])
    calls = _spy_grounded(monkeypatch)
    body = chat(client, "what is the xyzzyfnord treaty", ids)
    assert body["intent"] == "RETRIEVE"
    assert body["engine"] == "retrieve"
    assert body["sources"] == []
    assert body["answer"] == RETRIEVE_MISS_REPLY
    assert "?" not in body["answer"]
    assert calls == []
    assert _gap(ids) is None


def test_teleportation_policy_is_close_miss_skips_llm(client, ids, monkeypatch):
    calls = _spy_grounded(monkeypatch)
    body = chat(client, TELEPORT, ids)
    assert body["intent"] == "RETRIEVE"
    assert body["engine"] == "retrieve"
    assert body["sources"] == []
    assert body["answer"].startswith(RETRIEVE_MISS_REPLY)
    assert body["answer"] != RETRIEVE_MISS_REPLY
    assert "?" in body["answer"]
    assert REFUND_MARKER not in (body.get("answer") or "")
    assert "From refund_policy.md" not in (body.get("answer") or "")
    assert "DNE" not in body["answer"]
    assert calls == []
    gap = _gap(ids)
    assert gap and gap.get("query")
    assert gap.get("asked") is True
    assert "@" not in (gap.get("query") or "")


def test_weak_chunks_skip_llm_on_miss(client, ids, monkeypatch):
    weak = [
        {
            "source": "refund_policy.md",
            "content": f"Picnic wifi password is not a policy. {REFUND_MARKER}",
        }
    ]
    monkeypatch.setattr(retrieve, "search", lambda query, limit=5: list(weak))
    calls = _spy_grounded(monkeypatch)
    body = chat(client, "what is the teleportation policy", ids)
    assert body["sources"] == []
    assert REFUND_MARKER not in (body.get("answer") or "")
    assert body["answer"].startswith(RETRIEVE_MISS_REPLY)
    assert "?" in body["answer"]
    assert calls == []
    assert _gap(ids)


def test_close_miss_then_refund_followup_hits(client, ids):
    first = chat(client, TELEPORT, ids)
    assert "?" in first["answer"]
    body = chat(client, "refunds dude", ids)
    assert body["intent"] == "RETRIEVE"
    assert body["engine"] == "retrieve"
    assert body["sources"] == ["refund_policy.md"]
    assert REFUND_MARKER in (body.get("answer") or "")
    assert _gap(ids) is None


def test_second_miss_stops_diagnostic(client, ids, monkeypatch):
    chat(client, TELEPORT, ids)
    calls = _spy_grounded(monkeypatch)
    body = chat(client, "the space travel one lol", ids)
    assert body["intent"] == "RETRIEVE"
    assert body["sources"] == []
    assert body["answer"] == RETRIEVE_MISS_REPLY
    assert "?" not in body["answer"]
    assert calls == []
    assert _gap(ids) is None


def test_unrelated_hey_drops_retrieve_diagnostic(client, ids):
    chat(client, TELEPORT, ids)
    body = chat(client, "hey", ids)
    assert body["intent"] == "CHAT"
    assert body["engine"] == "chat"
    assert "sources" not in body
    assert body["answer"] == (
        "Hi! I'm Michelle in mock mode. No API credits are being used."
    )
    assert _gap(ids) is None


def test_nvm_drops_retrieve_diagnostic(client, ids):
    chat(client, TELEPORT, ids)
    body = chat(client, "nvm", ids)
    assert body["engine"] != "retrieve" or body.get("sources") == []
    assert _gap(ids) is None
    assert "?" not in (body.get("answer") or "")


def test_open_notes_after_close_miss_still_catalog_action(
    client, ids, fake_open, monkeypatch
):
    chat(client, TELEPORT, ids)
    monkeypatch.setattr(
        "main.classify_intent",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("classify_intent must not run on a catalog hit")
        ),
    )
    body = chat(client, "open Notes", ids)
    assert body["engine"] == "action"
    assert body["action_type"] == "open_app"
    assert body.get("routed_by") == "catalog"
    assert native_opens(fake_open) == [["open", "-a", "Notes"]]
    assert _gap(ids) is None


def test_force_intent_chat_drops_gap(client, ids):
    chat(client, TELEPORT, ids)
    resp = client.post(
        "/chat",
        json={
            "text": TELEPORT,
            "force_intent": "CHAT",
            "conversation_id": ids["conversation_id"],
            "user_id": ids["user_id"],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["engine"] == "chat"
    assert _gap(ids) is None


def test_retrieve_gap_not_in_system_prompt():
    from llm import _build_system_prompt

    prompt = _build_system_prompt(
        None,
        {
            "last_opened": [],
            "last_closed": [],
            "last_quit": [],
            "last_app_names": [],
            "last_action_type": None,
            "last_draft": None,
            "last_draft_pick": None,
            "last_retrieve_gap": {
                "query": "secret miss about teleportation",
                "asked": True,
            },
        },
    )
    assert "teleportation" not in prompt
    assert "secret miss" not in prompt


def test_email_stripped_from_retrieve_gap(client, ids):
    body = chat(client, "what is the teleportation policy for alex@example.com", ids)
    assert body["engine"] == "retrieve"
    gap = _gap(ids)
    if gap:
        assert "alex@example.com" not in (gap.get("query") or "")
        assert "@" not in (gap.get("query") or "")
