"""Track 2 — intent clarifying chips.

Ask only when INTENT_MODE=llm (not rules/mock), confidence is below 0.6,
and the top two shelf scores are close. Do not run ACTION/RETRIEVE until
the client sends force_intent.
"""

import actions
import intent
import main


def _close_scores(**overrides):
    base = {
        "intent": "RETRIEVE",
        "kind": "RETRIEVE",
        "confidence": 0.45,
        "is_question": True,
        "memory_score": 0.42,
        "docs_score": 0.48,
        "chat_score": 0.40,
    }
    return {**base, **overrides}


def test_should_clarify_when_low_confidence_and_top_two_close(monkeypatch):
    monkeypatch.setenv("INTENT_MODE", "llm")
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    assert intent.should_clarify_intent(_close_scores()) is True


def test_should_not_clarify_in_rules_mode(monkeypatch):
    monkeypatch.setenv("INTENT_MODE", "rules")
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    assert intent.should_clarify_intent(_close_scores()) is False


def test_should_not_clarify_in_mock_intent_mode(monkeypatch):
    monkeypatch.setenv("INTENT_MODE", "mock")
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    assert intent.should_clarify_intent(_close_scores()) is False


def test_should_not_clarify_when_confidence_is_high(monkeypatch):
    monkeypatch.setenv("INTENT_MODE", "llm")
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    assert intent.should_clarify_intent(_close_scores(confidence=0.72)) is False


def test_should_not_clarify_when_top_two_are_not_close(monkeypatch):
    monkeypatch.setenv("INTENT_MODE", "llm")
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    assert (
        intent.should_clarify_intent(
            _close_scores(docs_score=0.9, memory_score=0.1, chat_score=0.05)
        )
        is False
    )


def test_should_not_clarify_when_runner_up_score_is_zero(monkeypatch):
    monkeypatch.setenv("INTENT_MODE", "llm")
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    assert (
        intent.should_clarify_intent(
            _close_scores(docs_score=0.4, memory_score=0.0, chat_score=0.0)
        )
        is False
    )


def test_clarify_chips_cover_all_four_intents():
    labels = {row["intent"]: row["label"] for row in intent.CLARIFY_CHIPS}
    assert labels == {
        "RETRIEVE": "Look up docs",
        "REMEMBER": "Remember",
        "ACTION": "Do this",
        "CHAT": "Just chat",
    }


def test_rules_mode_chat_never_returns_clarify_engine(client, ids, monkeypatch):
    monkeypatch.setenv("INTENT_MODE", "rules")
    monkeypatch.setattr(main, "classify_intent", lambda *a, **k: _close_scores())
    r = client.post(
        "/chat",
        json={
            "text": "what's the refund policy?",
            "conversation_id": ids["conversation_id"],
            "user_id": ids["user_id"],
        },
    )
    body = r.json()
    assert body["engine"] != "clarify"
    assert not body.get("clarify_options")


def test_llm_mode_pauses_retrieve_until_chip(client, ids, monkeypatch):
    monkeypatch.setenv("INTENT_MODE", "llm")
    monkeypatch.setattr(main, "classify_intent", lambda *a, **k: _close_scores())

    first = client.post(
        "/chat",
        json={
            "text": "what's the refund policy?",
            "conversation_id": ids["conversation_id"],
            "user_id": ids["user_id"],
        },
    ).json()
    assert first["engine"] == "clarify"
    assert first["answer"]
    assert [row["intent"] for row in first["clarify_options"]] == [
        "RETRIEVE",
        "REMEMBER",
        "ACTION",
        "CHAT",
    ]
    assert "QUOKKABERRY-77" not in (first.get("answer") or "")
    assert not first.get("sources")
    assert not first.get("task_id")

    forced = client.post(
        "/chat",
        json={
            "text": "what's the refund policy?",
            "conversation_id": ids["conversation_id"],
            "user_id": ids["user_id"],
            "force_intent": "RETRIEVE",
        },
    ).json()
    assert forced["engine"] == "retrieve"
    assert "QUOKKABERRY-77" in (forced.get("answer") or "")

    hist = client.get(
        "/session/history",
        params={
            "conversation_id": ids["conversation_id"],
            "user_id": ids["user_id"],
        },
    ).json()
    user_turns = [m for m in hist["messages"] if m["role"] == "user"]
    assert [m["content"] for m in user_turns] == ["what's the refund policy?"]


def test_clarify_does_not_run_action(client, ids, monkeypatch):
    monkeypatch.setenv("INTENT_MODE", "llm")
    monkeypatch.setattr(
        main,
        "classify_intent",
        lambda *a, **k: _close_scores(intent="ACTION", kind="ACTION"),
    )
    opened = []
    monkeypatch.setattr(
        actions.NativeExecutor,
        "_open_app",
        lambda self, params: opened.append(params) or {"ok": True, "detail": "Opened Notes."},
    )
    body = client.post(
        "/chat",
        json={
            "text": "open Notes",
            "conversation_id": ids["conversation_id"],
            "user_id": ids["user_id"],
        },
    ).json()
    assert body["engine"] == "clarify"
    assert opened == []
    assert not body.get("task_id")
